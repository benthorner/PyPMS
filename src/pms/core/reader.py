"""
Read PM sensors

NOTE:
- Sensors are read on passive mode.
- Tested on PMS3003, PMS7003, PMSA003, SDS011 and MCU680
"""

import sys
import time
from abc import abstractmethod
from contextlib import AbstractContextManager, contextmanager
from csv import DictReader
from dataclasses import dataclass
from pathlib import Path
from textwrap import wrap
from typing import Iterator, NamedTuple, Optional, Union, overload

from serial import Serial
from typer import progressbar

from pms import InconsistentObservation, SensorNotReady, SensorWarmingUp, SensorWarning, logger
from pms.core import Sensor, Supported

from .types import ObsData

"""translation table for raw.hexdump(n)"""
HEXDUMP_TABLE = bytes.maketrans(
    bytes(range(0x20)) + bytes(range(0x7E, 0x100)), b"." * (0x20 + 0x100 - 0x7E)
)


class UnableToRead(Exception):
    pass


class TemporaryFailure(Exception):
    pass


class ReaderNotReady(Exception):
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


class Reader(AbstractContextManager):
    @abstractmethod
    def __call__(self, *, raw: Optional[bool]) -> Iterator[Union[RawData, ObsData]]:
        ...

    @abstractmethod
    def read_one(self) -> Reading:
        ...


class Sampler:
    # assert fields can be present
    last_reading: Optional[Reading]
    samples: Optional[int]

    def __init__(
        self,
        *,
        samples: Optional[int] = None,
        interval: Optional[int] = None,
    ):
        if samples is not None and samples < 1:
            # force at least one sample
            self.samples = 1
        else:
            self.samples = samples

        self.remaining_samples = self.samples
        self.interval = interval
        self.last_reading = None

    def sample(
        self,
        reading: Reading,
        *,
        raw: Optional[bool],
    ) -> Union[ObsData, RawData]:
        if self.last_reading and self.interval:
            delay = self.interval - (time.time() - self.last_reading.time)
            if delay > 0:
                time.sleep(delay)

        self.last_reading = reading

        if self.remaining_samples is not None:
            if self.remaining_samples <= 0:
                raise StopIteration

            self.remaining_samples -= 1

        return reading.raw_data if raw else reading.obs_data


class SensorReader(Reader):
    """Read sensor messages from serial port

    The sensor is woken up after opening the serial port, and put to sleep when before closing the port.
    While the serial port is open, the sensor is read in passive mode.

    PMS3003 sensors do not accept serial commands, such as wake/sleep or passive mode read.
    Valid messages are extracted from the serial buffer.
    """

    def __init__(
        self,
        sensor: Union[Sensor, Supported, str] = Supported.default,
        port: str = "/dev/ttyUSB0",
        interval: Optional[int] = None,
        samples: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> None:
        """Configure serial port"""
        self.sensor = sensor if isinstance(sensor, Sensor) else Sensor[sensor]
        self.pre_heat_seconds = self.sensor.pre_heat
        self.serial = Serial()
        self.serial.port = port
        self.serial.baudrate = self.sensor.baud
        self.serial.timeout = timeout or 5  # max time to wake up sensor
        self.interval = interval
        self.samples = samples
        logger.debug(
            f"capture {samples if samples else '?'} {sensor} obs "
            f"from {port} every {interval if interval else '?'} secs"
        )

    def _cmd(self, command: str) -> bytes:  # pragma: no cover
        """Write command to sensor and return answer"""

        # send command
        cmd = self.sensor.command(command)
        if cmd.command:
            self.serial.write(cmd.command)
            self.serial.flush()
        elif command.endswith("read"):
            self.serial.reset_input_buffer()

        # return full buffer
        return self.serial.read(max(cmd.answer_length, self.serial.in_waiting))

    def pre_heat(self):
        """Default implementation to wait for sensor"""
        if not self.pre_heat_seconds:
            return

        logger.info(f"pre-heating {self.sensor} sensor {self.pre_heat_seconds} sec")
        with progressbar(range(self.pre_heat_seconds), label="pre-heating") as progress:
            for _ in progress:
                time.sleep(1)

        # only pre-heat the first time
        self.pre_heat_seconds = 0

    def __enter__(self) -> "SensorReader":
        """Open serial port and sensor setup"""
        if not self.serial.is_open:
            logger.debug(f"open {self.serial.port}")
            self.serial.open()
            self.serial.reset_input_buffer()

        # wake sensor and set passive mode
        logger.debug(f"wake {self.sensor}")
        buffer = self._cmd("wake")
        self.pre_heat()
        buffer += self._cmd("passive_mode")
        logger.debug(f"buffer length: {len(buffer)}")

        # check if the sensor answered
        if len(buffer) == 0:  # pragma: no cover
            logger.error(f"Sensor did not respond, check UART pin connections")
            raise UnableToRead("Sensor did not respond")

        # check against sensor type derived from buffer
        if not self.sensor.check(buffer, "passive_mode"):  # pragma: no cover
            logger.error(f"Sensor is not {self.sensor.name}")
            raise UnableToRead("Sensor failed validation")

        return self

    def __exit__(self, exception_type, exception_value, traceback) -> None:
        """Put sensor to sleep and close serial port"""
        logger.debug(f"sleep {self.sensor}")
        buffer = self._cmd("sleep")
        logger.debug(f"close {self.serial.port}")
        self.serial.close()

    def __call__(self, *, raw: Optional[bool] = None):
        """Passive mode reading at regular intervals"""

        sampler = Sampler(
            samples=self.samples,
            interval=self.interval,
        )

        try:
            while True:
                try:
                    reading = self.read_one()
                except ReaderNotReady as e:  # pragma: no cover
                    logger.debug(e)
                    time.sleep(5)
                except TemporaryFailure as e:  # pragma: no cover
                    logger.debug(e)
                else:
                    yield sampler.sample(reading, raw=raw)
        except KeyboardInterrupt:  # pragma: no cover
            print()
        except StopIteration:
            return

    def read_one(self) -> Reading:
        if not self.serial.is_open:
            raise StopIteration

        buffer = self._cmd("passive_read")

        try:
            obs = self.sensor.decode(buffer)
            return Reading(buffer=buffer, obs_data=obs)
        except SensorNotReady as e:  # pragma: no cover
            # no special handling needed
            raise ReaderNotReady
        except SensorWarning as e:  # pragma: no cover
            self.serial.reset_input_buffer()
            raise TemporaryFailure


class MessageReader(Reader):
    def __init__(self, path: Path, sensor: Sensor, samples: Optional[int] = None) -> None:
        self.path = path
        self.sensor = sensor
        self.sampler = Sampler(samples=samples)

    def __enter__(self) -> "MessageReader":
        logger.debug(f"open {self.path}")
        self.csv = self.path.open()
        reader = DictReader(self.csv)
        self.data = (row for row in reader if row["sensor"] == self.sensor.name)
        return self

    def __exit__(self, exception_type, exception_value, traceback) -> None:
        logger.debug(f"close {self.path}")
        self.csv.close()

    def __call__(self, *, raw: Optional[bool] = None):
        try:
            while True:
                reading = self.read_one()
                yield self.sampler.sample(reading, raw=raw)
        except StopIteration:
            return

    def read_one(self) -> Reading:
        row = next(self.data)
        time, message = int(row["time"]), bytes.fromhex(row["hex"])
        obs = self.sensor.decode(message, time=time)
        return Reading(buffer=message, obs_data=obs)


@contextmanager
def exit_on_fail(reader: Reader):
    try:
        with reader:
            yield reader
    except UnableToRead:
        sys.exit(1)
