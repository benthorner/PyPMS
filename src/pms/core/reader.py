"""
Read PM sensors

NOTE:
- Sensors are read on passive mode.
- Tested on PMS3003, PMS7003, PMSA003, SDS011 and MCU680
"""

import sys
import time
from abc import abstractmethod
from contextlib import contextmanager
from csv import DictReader
from dataclasses import dataclass
from pathlib import Path
from textwrap import wrap
from typing import Iterator, NamedTuple, Optional, Union, overload

from serial import Serial
from typer import progressbar

from pms import SensorNotReady, SensorWarning, logger
from pms.core import Sensor, Supported

from .types import ObsData

"""translation table for raw.hexdump(n)"""
HEXDUMP_TABLE = bytes.maketrans(
    bytes(range(0x20)) + bytes(range(0x7E, 0x100)), b"." * (0x20 + 0x100 - 0x7E)
)


class UnableToRead(Exception):
    pass


class RawData(NamedTuple):
    """raw messages with timestamp"""

    time: int
    data: bytes

    @property
    def hex(self) -> str:
        return self.data.hex()

    def hexdump(self, line: Optional[int] = None) -> str:
        offset = time if line is None else line * len(self.data)
        hex = " ".join(wrap(self.data.hex(), 2))  # raw.hex(" ") in python3.8+
        dump = self.data.translate(HEXDUMP_TABLE).decode()
        return f"{offset:08x}: {hex}  {dump}"


@dataclass
class Reading:
    buffer: bytes
    obs_data: ObsData

    @property
    def raw_data(self) -> RawData:
        return RawData(self.time, self.buffer)

    @property
    def time(self) -> int:
        return self.obs_data.time


class Stream:
    """
    Standard interface to read data.
    """

    @abstractmethod
    def read(self) -> Reading:
        ...

    @abstractmethod
    def open(self) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...


class Reader:
    def __init__(self, *, stream: Stream) -> None:
        self.stream = stream

    @abstractmethod
    def __call__(self, *, raw: Optional[bool]) -> Iterator[Union[RawData, ObsData]]:
        """
        Return an iterator of ObsData.

        If "raw" is set to True, then ObsData is replaced with RawData.
        """
        ...

    def __enter__(self):
        self.stream.open()
        return self

    def __exit__(self, *args) -> None:
        self.stream.close()


class SensorStream(Stream):
    """Read sensor messages from serial port

    PMS3003 sensors do not accept serial commands, such as wake/sleep or passive mode read.
    Valid messages are extracted from the serial buffer.
    """

    def __init__(
        self,
        *,
        sensor: Union[Sensor, Supported, str] = Supported.default,
        port: str = "/dev/ttyUSB0",
        timeout: Optional[float] = None,
    ) -> None:
        """Configure serial port"""
        self.sensor = sensor if isinstance(sensor, Sensor) else Sensor[sensor]
        self.pre_heat = self.sensor.pre_heat
        self.serial = Serial()
        self.serial.port = port
        self.serial.baudrate = self.sensor.baud
        self.serial.timeout = timeout or 5  # max time to wake up sensor

    def _cmd(self, command: str) -> bytes:
        """Write command to sensor and return answer"""

        # send command
        cmd = self.sensor.command(command)
        if cmd.command:
            self.serial.write(cmd.command)
            self.serial.flush()
        elif command.endswith("read"):  # pragma: no cover
            self.serial.reset_input_buffer()

        # return full buffer
        return self.serial.read(max(cmd.answer_length, self.serial.in_waiting))

    def _pre_heat(self):
        if not self.pre_heat:
            return

        logger.info(f"pre-heating {self.sensor} sensor {self.pre_heat} sec")
        with progressbar(range(self.pre_heat), label="pre-heating") as progress:
            for _ in progress:
                time.sleep(1)

        # only pre-heat the firs time
        self.pre_heat = 0

    def read(self) -> Reading:
        """Return a single passive mode reading"""

        if not self.serial.is_open:
            raise StopIteration

        buffer = self._cmd("passive_read")

        try:
            obs = self.sensor.decode(buffer)
            return Reading(buffer=buffer, obs_data=obs)
        except SensorNotReady as e:
            # no special hardware handling
            raise
        except SensorWarning as e:  # pragma: no cover
            self.serial.reset_input_buffer()
            raise

    def open(self) -> None:
        """Open serial port and sensor setup"""
        if not self.serial.is_open:
            logger.debug(f"open {self.serial.port}")
            self.serial.open()
            self.serial.reset_input_buffer()

        # wake sensor and set passive mode
        logger.debug(f"wake {self.sensor}")
        buffer = self._cmd("wake")
        self._pre_heat()
        buffer += self._cmd("passive_mode")
        logger.debug(f"buffer length: {len(buffer)}")

        # check if the sensor answered
        if len(buffer) == 0:
            logger.error(f"Sensor did not respond, check UART pin connections")
            raise UnableToRead("Sensor did not respond")

        # check against sensor type derived from buffer
        if not self.sensor.check(buffer, "passive_mode"):
            logger.error(f"Sensor is not {self.sensor.name}")
            raise UnableToRead("Sensor failed validation")

    def close(self) -> None:
        """Put sensor to sleep and close serial port"""
        logger.debug(f"sleep {self.sensor}")
        buffer = self._cmd("sleep")
        logger.debug(f"close {self.serial.port}")
        self.serial.close()


class SensorReader(Reader):
    """Read sensor messages from serial port

    The sensor is woken up after opening the serial port, and put to sleep when before closing the port.
    While the serial port is open, the sensor is read in passive mode.
    """

    def __init__(
        self,
        sensor: Union[Sensor, Supported, str] = Supported.default,
        port: str = "/dev/ttyUSB0",
        interval: Optional[int] = None,
        samples: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> None:
        super().__init__(
            stream=SensorStream(
                sensor=sensor,
                port=port,
                timeout=timeout,
            )
        )

        self.interval = interval
        self.samples = samples
        logger.debug(
            f"capture {samples if samples else '?'} {sensor} obs "
            f"from {port} every {interval if interval else '?'} secs"
        )

    @property
    def sensor(self):
        return self.stream.sensor

    def __call__(self, *, raw: Optional[bool] = None):
        """Passive mode reading at regular intervals"""

        sample = 0
        try:
            while True:
                try:
                    reading = self.stream.read()
                except SensorNotReady as e:
                    logger.debug(e)
                    time.sleep(5)
                except SensorWarning as e:
                    logger.debug(e)
                else:
                    yield reading.raw_data if raw else reading.obs_data
                    sample += 1
                    if self.samples is not None and sample >= self.samples:
                        break
                    if self.interval:
                        delay = self.interval - (time.time() - reading.time)
                        if delay > 0:
                            time.sleep(delay)
        except KeyboardInterrupt:  # pragma: no cover
            print()
        except StopIteration:
            pass


class MessageStream(Stream):
    def __init__(self, *, path: Path, sensor: Sensor) -> None:
        self.path = path
        self.sensor = sensor

    def read(self) -> Reading:
        if not hasattr(self, "data"):
            raise StopIteration

        row = next(self.data)
        time, message = int(row["time"]), bytes.fromhex(row["hex"])
        obs = self.sensor.decode(message, time=time)
        return Reading(buffer=message, obs_data=obs)

    def open(self) -> None:
        logger.debug(f"open {self.path}")
        self.csv = self.path.open()
        reader = DictReader(self.csv)
        self.data = (row for row in reader if row["sensor"] == self.sensor.name)

    def close(self) -> None:
        logger.debug(f"close {self.path}")
        self.csv.close()


class MessageReader(Reader):
    def __init__(self, path: Path, sensor: Sensor, samples: Optional[int] = None) -> None:
        super().__init__(
            stream=MessageStream(path=path, sensor=sensor),
        )

        self.samples = samples

    def __call__(self, *, raw: Optional[bool] = None):
        try:
            while True:
                reading = self.stream.read()
                yield reading.raw_data if raw else reading.obs_data
                if self.samples:
                    self.samples -= 1
                    if self.samples <= 0:
                        break
        except StopIteration:
            return


@contextmanager
def exit_on_fail(reader: Reader):
    try:
        with reader:
            yield reader
    except UnableToRead:
        sys.exit(1)
