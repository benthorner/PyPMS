import pytest

import pms
from pms.core import reader
from pms.core.sensor import Sensor
from tests.conftest import captured_data


class MockReader(reader.Reader):
    def __init__(self, raise_on_enter=False):
        self.raise_on_enter = raise_on_enter

    def __call__(self):
        raise NotImplemented

    def open(self):
        if self.raise_on_enter:
            raise reader.UnableToRead()
        self.entered = True

    def close(self):
        self.exited = True


@pytest.fixture
def mock_sleep(monkeypatch):
    def sleep(seconds):
        sleep.slept_for += seconds

    sleep.slept_for = 0

    monkeypatch.setattr(
        reader.time,
        "sleep",
        sleep,
    )

    return sleep


@pytest.fixture
def mock_sensor(mock_serial):
    mock_serial.stub(
        name="wake",
        receive_bytes=b"BM\xe4\x00\x01\x01t",
        send_bytes=(
            b"BM\x00\x1c"  # expected header
            + b".........................."  # payload (to total 32 bytes)
            + b"\x05W"  # checksum = sum(header) + sum(payload)
        ),
    )

    mock_serial.stub(
        name="passive_mode",
        receive_bytes=b"BM\xe1\x00\x00\x01p",
        send_bytes=(
            b"BM\x00\x04"  # expected header
            + b".."  # payload (to total 8 bytes)
            + b"\x00\xef"  # checksum
        ),
    )

    mock_serial.stub(
        name="passive_read",
        receive_bytes=b"BM\xe2\x00\x00\x01q",
        send_bytes=(
            b"BM\x00\x1c"  # expected header
            + b".........................."  # payload (to total 32 bytes)
            + b"\x05W"  # checksum
        ),
    )

    mock_serial.stub(
        name="sleep",
        receive_bytes=b"BM\xe4\x00\x00\x01s",
        send_bytes=(
            b"BM\x00\x04"  # expected header
            + b".."  # payload (to total 8 bytes)
            + b"\x00\xef"  # checksum
        ),
    )

    return mock_serial


@pytest.fixture
def mock_sensor_warm_up(mock_serial):
    def passive_read(n):
        if n == 1:
            # first return a "0" payload ("warming up")
            return (
                b"BM\x00\x1c"  # expected header
                + b"\0" * 26  # payload (to total 32 bytes)
                + b"\x00\xAB"  # checksum
            )
        else:
            # then behave like the original stub again
            return (
                b"BM\x00\x1c"  # expected header
                + b".........................."  # payload
                + b"\x05W"  # checksum
            )

    mock_serial.stub(
        name="passive_read",
        receive_bytes=b"BM\xe2\x00\x00\x01q",
        send_fn=passive_read,
    )


@pytest.fixture
def mock_sensor_temp_failure(mock_serial):
    def passive_read(n):
        if n == 1:
            # first return garbage data (bad checksum)
            return (
                b"BM\x00\x1c"  # expected header
                + b"\0" * 26  # payload (to total 32 bytes)
                + b"\x00\xFF"  # checksum
            )
        else:
            # then behave like the original stub again
            return (
                b"BM\x00\x1c"  # expected header
                + b".........................."  # payload
                + b"\x05W"  # checksum
            )

    mock_serial.stub(
        name="passive_read",
        receive_bytes=b"BM\xe2\x00\x00\x01q",
        send_fn=passive_read,
    )


@pytest.fixture
def sensor_reader_factory(monkeypatch, mock_sensor):
    def factory(
        *,
        samples=0,  # exit immediately
        interval=None,
        sensor="PMSx003",  # match with stubs
        max_retries=None,
    ):
        sensor_reader = reader.SensorReader(
            port=mock_sensor.port,
            samples=samples,
            interval=interval,
            sensor=sensor,
            timeout=0.01,  # low to avoid hanging on failure
            max_retries=max_retries,
        )

        # https://github.com/pyserial/pyserial/issues/625
        monkeypatch.setattr(
            sensor_reader.serial,
            "flush",
            lambda: None,
        )

        return sensor_reader

    return factory


def test_sensor_reader(mock_sensor, sensor_reader_factory):
    sensor_reader = sensor_reader_factory()

    with sensor_reader as r:
        obs = list(r())

    # check warm up happened
    assert mock_sensor.stubs["wake"].called
    assert mock_sensor.stubs["passive_mode"].called

    # check data was read
    assert len(obs) == 1
    assert obs[0].pm10 == 11822

    # check sleep happened
    assert mock_sensor.stubs["sleep"].called


def test_sensor_reader_sleep(sensor_reader_factory, mock_sleep):
    sensor_reader = sensor_reader_factory(
        samples=2,  # try to read twice
        interval=5,  # sleep between samples
    )

    with sensor_reader as r:
        obs = list(r())

    # check we read twice
    assert len(obs) == 2

    # check we slept between reads
    assert 0 < mock_sleep.slept_for < 5


def test_sensor_reader_closed(mock_sensor, sensor_reader_factory):
    sensor_reader = sensor_reader_factory()
    obs = list(sensor_reader())
    assert len(obs) == 0


def test_sensor_reader_preheat(sensor_reader_factory, mock_sleep):
    sensor_reader = sensor_reader_factory()

    # override pre heat duration
    sensor_reader.pre_heat = 5

    with sensor_reader as r:
        pass

    # check we slept between reads
    assert mock_sleep.slept_for == 5


def test_sensor_reader_warm_up(
    mock_sensor,
    sensor_reader_factory,
    mock_sleep,
    mock_sensor_warm_up,
):
    sensor_reader = sensor_reader_factory()

    with sensor_reader as r:
        obs = list(r())

    # check we slept for warm up
    assert mock_sleep.slept_for == 5
    assert len(obs) == 1


def test_sensor_reader_warm_up_exhaust_retries(
    mock_sensor,
    sensor_reader_factory,
    mock_sensor_warm_up,
):
    sensor_reader = sensor_reader_factory(max_retries=0)

    with sensor_reader as r:
        with pytest.raises(pms.SensorWarmingUp):
            list(r())


def test_sensor_reader_temp_failure(
    mock_sensor,
    sensor_reader_factory,
    mock_sensor_temp_failure,
):
    sensor_reader = sensor_reader_factory()

    with sensor_reader as r:
        obs = list(r())

    # check one sample still acquired
    assert len(obs) == 1

    # check two samples were attempted
    assert mock_sensor.stubs["passive_read"].calls == 2


def test_sensor_reader_temp_failure_exhaust_retries(
    mock_sensor,
    sensor_reader_factory,
    mock_sensor_temp_failure,
):
    sensor_reader = sensor_reader_factory(max_retries=0)

    with sensor_reader as r:
        with pytest.raises(pms.SensorWarning):
            list(r())


def test_sensor_reader_sensor_mismatch(mock_sensor, sensor_reader_factory):
    sensor_reader = sensor_reader_factory()

    mock_sensor.stub(
        name="passive_mode",  # used for validation
        receive_bytes=b"BM\xe1\x00\x00\x01p",
        send_bytes=b"123",  # nonsense
    )

    with pytest.raises(reader.UnableToRead) as e:
        with sensor_reader as r:
            list(r())

    assert "failed validation" in str(e.value)


def test_sensor_reader_sensor_no_response(sensor_reader_factory):
    sensor_reader = sensor_reader_factory(
        sensor="PMS3003",  # arbitrary sensor
    )

    with pytest.raises(reader.UnableToRead) as e:
        with sensor_reader as r:
            list(r())

    assert "did not respond" in str(e.value)


def test_exit_on_fail_no_error(monkeypatch):
    # prevent the helper exiting the test suite
    monkeypatch.setattr(reader.sys, "exit", lambda: None)
    mock_reader = MockReader()

    with reader.exit_on_fail(mock_reader) as yielded:
        assert yielded == mock_reader

    assert mock_reader.entered
    assert mock_reader.exited


def test_exit_on_fail_error(monkeypatch):
    def sys_exit(*_args):
        raise Exception("exit")

    # prevent the helper exiting the test suite
    monkeypatch.setattr(reader.sys, "exit", sys_exit)
    mock_reader = MockReader(raise_on_enter=True)

    with pytest.raises(Exception) as e:
        with reader.exit_on_fail(mock_reader):
            raise Exception("should not get here")

    assert "exit" in str(e.value)


def test_message_reader():
    message_reader = reader.MessageReader(
        path=captured_data,
        sensor=Sensor["PMS3003"],
    )

    with message_reader:
        values = list(message_reader())

    assert len(values) == 10


def test_message_reader_closed():
    message_reader = reader.MessageReader(
        path=captured_data,
        sensor=Sensor["PMS3003"],
    )

    values = list(message_reader())
    assert len(values) == 0
