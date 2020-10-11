from typer import Context, Option

from pms.service.influxdb import client_pub, influxdb
from pms.service.mqtt import Data, client_sub, mqtt


def bridge(
    mqtt_topic: str = Option("homie/+/+/+", help="mqtt root/topic"),
    mqtt_host: str = Option("mqtt.eclipse.org", help="mqtt server"),
    mqtt_port: int = Option(1883, help="server port"),
    mqtt_user: str = Option("", help="server username", show_default=False),
    mqtt_pass: str = Option("", help="server password", show_default=False),
    db_host: str = Option("influxdb", help="database server"),
    db_port: int = Option(8086, help="server port"),
    db_user: str = Option("root", help="server username"),
    db_pass: str = Option("root", help="server password"),
    db_name: str = Option("homie", help="database name"),
):
    """Bridge between MQTT and InfluxDB servers"""
    pub = client_pub(
        host=db_host, port=db_port, username=db_user, password=db_pass, db_name=db_name
    )

    def on_sensordata(data: Data) -> None:
        pub(time=data.time, tags={"location": data.location}, data={data.measurement: data.value})

    client_sub(
        topic=mqtt_topic,
        host=mqtt_host,
        port=mqtt_port,
        username=mqtt_user,
        password=mqtt_pass,
        on_sensordata=on_sensordata,
    )
