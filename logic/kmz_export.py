import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple
from xml.dom import minidom
import zipfile


@dataclass
class MissionConfig:
    drone_type: str = "M300"
    takeoff_security_height: float = 20.0
    global_rth_height: float = 50.0
    global_transitional_speed: float = 15.0
    auto_flight_speed: float = 5.0
    execute_height: float = 3.0
    waypoint_heading_mode: str = "followWayline"
    waypoint_heading_angle: Optional[float] = None
    image_format: Optional[str] = None


def _drone_payload_info(drone_type: str) -> Tuple[int, int, int, int, int]:
    # Reused mapping strategy from kmzWithQfUi project.
    if drone_type == "M3T":
        return 77, 1, 67, 0, 0
    if drone_type == "M300":
        return 60, 0, 50, 0, 0
    raise ValueError(f"不支持的无人机型号: {drone_type}")


def _validate_export_config(
    config: MissionConfig,
    pitch: Optional[float],
    yaw: Optional[float],
) -> None:
    _drone_payload_info(config.drone_type)

    if not 1.2 <= config.takeoff_security_height <= 1500:
        raise ValueError("起飞安全高度必须在 1.2～1500 m 之间")
    if not 2 <= config.global_rth_height <= 1500:
        raise ValueError("返航高度必须在 2～1500 m 之间")
    if not 0 <= config.global_transitional_speed <= 15:
        raise ValueError("首段过渡速度必须在 0～15 m/s 之间")
    if not 1 <= config.auto_flight_speed <= 15:
        raise ValueError("自动飞行速度必须在 1～15 m/s 之间")
    if config.execute_height <= 0:
        raise ValueError("执行高度必须大于 0 m")

    if config.waypoint_heading_mode not in {"followWayline", "fixed"}:
        raise ValueError("当前仅支持跟随航线或固定航向模式")
    if config.waypoint_heading_mode == "fixed" and config.waypoint_heading_angle is None:
        raise ValueError("固定航向模式必须设置飞机航向角")
    if config.waypoint_heading_angle is not None and not -180 <= config.waypoint_heading_angle <= 180:
        raise ValueError("飞机航向角必须在 -180°～180° 之间")

    pitch_ranges = {
        "M300": (-120.0, 45.0),
        "M3T": (-90.0, 35.0),
    }
    if pitch is not None:
        minimum, maximum = pitch_ranges[config.drone_type]
        if not minimum <= pitch <= maximum:
            raise ValueError(
                f"{config.drone_type} 云台俯仰角必须在 {minimum:g}°～{maximum:g}° 之间"
            )

    if config.drone_type == "M3T" and yaw is not None:
        raise ValueError("M3T 云台偏航不可独立控制，请改用飞机航向角")
    if yaw is not None and not -180 <= yaw <= 180:
        raise ValueError("云台偏航角必须在 -180°～180° 之间")

    if config.drone_type == "M3T":
        image_format = config.image_format or "wide,ir"
        formats = image_format.split(",")
        if not formats or any(value not in {"wide", "zoom", "ir"} for value in formats):
            raise ValueError("M3T 拍照镜头配置无效")


def _pretty_xml(elem: ET.Element) -> str:
    rough = ET.tostring(elem, "utf-8")
    parsed = minidom.parseString(rough)
    return parsed.toprettyxml(indent="  ")


def _build_template_kml(
    waypoints: Sequence[Tuple[float, float]],
    config: MissionConfig,
    pitch: Optional[float],
    yaw: Optional[float],
) -> ET.Element:
    drone_enum, drone_sub, payload_enum, payload_sub, payload_pos = _drone_payload_info(config.drone_type)

    kml = ET.Element(
        "kml",
        {
            "xmlns": "http://www.opengis.net/kml/2.2",
            "xmlns:wpml": "http://www.dji.com/wpmz/1.0.6",
        },
    )
    document = ET.SubElement(kml, "Document")

    now_ms = str(int(time.time() * 1000))
    ET.SubElement(document, "wpml:createTime").text = now_ms
    ET.SubElement(document, "wpml:updateTime").text = now_ms

    mission = ET.SubElement(document, "wpml:missionConfig")
    ET.SubElement(mission, "wpml:flyToWaylineMode").text = "safely"
    ET.SubElement(mission, "wpml:finishAction").text = "goHome"
    ET.SubElement(mission, "wpml:exitOnRCLost").text = "executeLostAction"
    ET.SubElement(mission, "wpml:executeRCLostAction").text = "goBack"
    ET.SubElement(mission, "wpml:takeOffSecurityHeight").text = str(config.takeoff_security_height)
    ET.SubElement(mission, "wpml:globalRTHHeight").text = str(config.global_rth_height)
    ET.SubElement(mission, "wpml:globalTransitionalSpeed").text = str(config.global_transitional_speed)

    drone_info = ET.SubElement(mission, "wpml:droneInfo")
    ET.SubElement(drone_info, "wpml:droneEnumValue").text = str(drone_enum)
    ET.SubElement(drone_info, "wpml:droneSubEnumValue").text = str(drone_sub)

    payload_info = ET.SubElement(mission, "wpml:payloadInfo")
    ET.SubElement(payload_info, "wpml:payloadEnumValue").text = str(payload_enum)
    ET.SubElement(payload_info, "wpml:payloadSubEnumValue").text = str(payload_sub)
    ET.SubElement(payload_info, "wpml:payloadPositionIndex").text = str(payload_pos)

    folder = ET.SubElement(document, "Folder")
    ET.SubElement(folder, "wpml:templateType").text = "waypoint"
    ET.SubElement(folder, "wpml:templateId").text = "0"

    coord_param = ET.SubElement(folder, "wpml:waylineCoordinateSysParam")
    ET.SubElement(coord_param, "wpml:coordinateMode").text = "WGS84"
    ET.SubElement(coord_param, "wpml:heightMode").text = "relativeToStartPoint"
    ET.SubElement(coord_param, "wpml:positioningType").text = "GPS"

    ET.SubElement(folder, "wpml:autoFlightSpeed").text = str(config.auto_flight_speed)
    ET.SubElement(folder, "wpml:globalHeight").text = str(config.execute_height)
    ET.SubElement(folder, "wpml:caliFlightEnable").text = "0"
    ET.SubElement(folder, "wpml:gimbalPitchMode").text = "manual"

    heading = ET.SubElement(folder, "wpml:globalWaypointHeadingParam")
    ET.SubElement(heading, "wpml:waypointHeadingMode").text = config.waypoint_heading_mode
    ET.SubElement(heading, "wpml:waypointHeadingAngle").text = str(
        config.waypoint_heading_angle if config.waypoint_heading_angle is not None else 0
    )
    ET.SubElement(heading, "wpml:waypointPoiPoint").text = "0.000000,0.000000,0.000000"
    ET.SubElement(heading, "wpml:waypointHeadingPathMode").text = "followBadArc"
    ET.SubElement(heading, "wpml:waypointHeadingPoiIndex").text = "0"

    ET.SubElement(folder, "wpml:globalWaypointTurnMode").text = "toPointAndStopWithDiscontinuityCurvature"
    ET.SubElement(folder, "wpml:globalUseStraightLine").text = "1"

    for idx, (lon, lat) in enumerate(waypoints):
        placemark = ET.SubElement(folder, "Placemark")
        point = ET.SubElement(placemark, "Point")
        ET.SubElement(point, "coordinates").text = f"{lon:.8f},{lat:.8f}"

        index_text = str(idx)
        ET.SubElement(placemark, "wpml:index").text = index_text
        ET.SubElement(placemark, "wpml:ellipsoidHeight").text = str(config.execute_height)
        ET.SubElement(placemark, "wpml:height").text = str(config.execute_height)
        ET.SubElement(placemark, "wpml:useGlobalHeight").text = "1"
        ET.SubElement(placemark, "wpml:useGlobalSpeed").text = "1"
        ET.SubElement(placemark, "wpml:useGlobalHeadingParam").text = "1"
        ET.SubElement(placemark, "wpml:useGlobalTurnParam").text = "1"
        ET.SubElement(placemark, "wpml:useStraightLine").text = "0"

        action_group = ET.SubElement(placemark, "wpml:actionGroup")
        ET.SubElement(action_group, "wpml:actionGroupId").text = index_text
        ET.SubElement(action_group, "wpml:actionGroupStartIndex").text = index_text
        ET.SubElement(action_group, "wpml:actionGroupEndIndex").text = index_text
        ET.SubElement(action_group, "wpml:actionGroupMode").text = "sequence"

        trigger = ET.SubElement(action_group, "wpml:actionTrigger")
        ET.SubElement(trigger, "wpml:actionTriggerType").text = "reachPoint"

        action_id = -1

        if pitch is not None:
            action_id += 1
            action = ET.SubElement(action_group, "wpml:action")
            ET.SubElement(action, "wpml:actionId").text = str(action_id)
            ET.SubElement(action, "wpml:actionActuatorFunc").text = "gimbalRotate"
            param = ET.SubElement(action, "wpml:actionActuatorFuncParam")
            ET.SubElement(param, "wpml:gimbalHeadingYawBase").text = "north"
            ET.SubElement(param, "wpml:gimbalRotateMode").text = "absoluteAngle"
            ET.SubElement(param, "wpml:gimbalPitchRotateEnable").text = "1"
            ET.SubElement(param, "wpml:gimbalPitchRotateAngle").text = str(pitch)
            ET.SubElement(param, "wpml:gimbalRollRotateEnable").text = "0"
            ET.SubElement(param, "wpml:gimbalRollRotateAngle").text = "0"
            ET.SubElement(param, "wpml:gimbalYawRotateEnable").text = "0"
            ET.SubElement(param, "wpml:gimbalYawRotateAngle").text = "0"
            ET.SubElement(param, "wpml:gimbalRotateTimeEnable").text = "0"
            ET.SubElement(param, "wpml:gimbalRotateTime").text = "0"
            ET.SubElement(param, "wpml:payloadPositionIndex").text = str(payload_pos)

        if yaw is not None:
            action_id += 1
            action = ET.SubElement(action_group, "wpml:action")
            ET.SubElement(action, "wpml:actionId").text = str(action_id)
            ET.SubElement(action, "wpml:actionActuatorFunc").text = "gimbalRotate"
            param = ET.SubElement(action, "wpml:actionActuatorFuncParam")
            ET.SubElement(param, "wpml:gimbalHeadingYawBase").text = "north"
            ET.SubElement(param, "wpml:gimbalRotateMode").text = "absoluteAngle"
            ET.SubElement(param, "wpml:gimbalPitchRotateEnable").text = "0"
            ET.SubElement(param, "wpml:gimbalPitchRotateAngle").text = "0"
            ET.SubElement(param, "wpml:gimbalRollRotateEnable").text = "0"
            ET.SubElement(param, "wpml:gimbalRollRotateAngle").text = "0"
            ET.SubElement(param, "wpml:gimbalYawRotateEnable").text = "1"
            ET.SubElement(param, "wpml:gimbalYawRotateAngle").text = str(yaw)
            ET.SubElement(param, "wpml:gimbalRotateTimeEnable").text = "0"
            ET.SubElement(param, "wpml:gimbalRotateTime").text = "0"
            ET.SubElement(param, "wpml:payloadPositionIndex").text = str(payload_pos)

        action_id += 1
        photo_action = ET.SubElement(action_group, "wpml:action")
        ET.SubElement(photo_action, "wpml:actionId").text = str(action_id)
        ET.SubElement(photo_action, "wpml:actionActuatorFunc").text = "takePhoto"
        photo_param = ET.SubElement(photo_action, "wpml:actionActuatorFuncParam")
        ET.SubElement(photo_param, "wpml:fileSuffix").text = f"航点{idx + 1}"
        ET.SubElement(photo_param, "wpml:payloadPositionIndex").text = str(payload_pos)
        ET.SubElement(photo_param, "wpml:useGlobalPayloadLensIndex").text = (
            "1" if config.drone_type == "M3T" else "0"
        )

        if config.drone_type != "M3T":
            ET.SubElement(placemark, "wpml:isRisky").text = "0"

    payload_param = ET.SubElement(folder, "wpml:payloadParam")
    ET.SubElement(payload_param, "wpml:payloadPositionIndex").text = str(payload_pos)
    if config.drone_type == "M3T":
        ET.SubElement(payload_param, "wpml:imageFormat").text = (
            config.image_format or "wide,ir"
        )
    else:
        ET.SubElement(payload_param, "wpml:meteringMode").text = "average"
        ET.SubElement(payload_param, "wpml:dewarpingEnable").text = "0"
        ET.SubElement(payload_param, "wpml:returnMode").text = "singleReturnStrongest"
        ET.SubElement(payload_param, "wpml:samplingRate").text = "240000"
        ET.SubElement(payload_param, "wpml:scanningMode").text = "nonRepetitive"
        ET.SubElement(payload_param, "wpml:modelColoringEnable").text = "0"

    return kml


def _build_waylines_wpml(
    waypoints: Sequence[Tuple[float, float]],
    config: MissionConfig,
    pitch: Optional[float],
    yaw: Optional[float],
) -> ET.Element:
    drone_enum, drone_sub, payload_enum, payload_sub, payload_pos = _drone_payload_info(
        config.drone_type
    )

    wpml = ET.Element(
        "kml",
        {
            "xmlns": "http://www.opengis.net/kml/2.2",
            "xmlns:wpml": "http://www.dji.com/wpmz/1.0.6",
        },
    )
    document = ET.SubElement(wpml, "Document")

    mission = ET.SubElement(document, "wpml:missionConfig")
    ET.SubElement(mission, "wpml:flyToWaylineMode").text = "safely"
    ET.SubElement(mission, "wpml:finishAction").text = "goHome"
    ET.SubElement(mission, "wpml:exitOnRCLost").text = "executeLostAction"
    ET.SubElement(mission, "wpml:executeRCLostAction").text = "goBack"
    ET.SubElement(mission, "wpml:takeOffSecurityHeight").text = str(config.takeoff_security_height)
    ET.SubElement(mission, "wpml:globalRTHHeight").text = str(config.global_rth_height)
    ET.SubElement(mission, "wpml:globalTransitionalSpeed").text = str(config.global_transitional_speed)

    drone_info = ET.SubElement(mission, "wpml:droneInfo")
    ET.SubElement(drone_info, "wpml:droneEnumValue").text = str(drone_enum)
    ET.SubElement(drone_info, "wpml:droneSubEnumValue").text = str(drone_sub)

    payload_info = ET.SubElement(mission, "wpml:payloadInfo")
    ET.SubElement(payload_info, "wpml:payloadEnumValue").text = str(payload_enum)
    ET.SubElement(payload_info, "wpml:payloadSubEnumValue").text = str(payload_sub)
    ET.SubElement(payload_info, "wpml:payloadPositionIndex").text = str(payload_pos)

    folder = ET.SubElement(document, "Folder")
    ET.SubElement(folder, "wpml:templateId").text = "0"
    ET.SubElement(folder, "wpml:executeHeightMode").text = "relativeToStartPoint"
    ET.SubElement(folder, "wpml:waylineId").text = "0"
    ET.SubElement(folder, "wpml:autoFlightSpeed").text = str(config.auto_flight_speed)

    for idx, (lon, lat) in enumerate(waypoints):
        placemark = ET.SubElement(folder, "Placemark")
        point = ET.SubElement(placemark, "Point")
        ET.SubElement(point, "coordinates").text = f"{lon:.8f},{lat:.8f}"

        index_text = str(idx)
        ET.SubElement(placemark, "wpml:index").text = index_text
        ET.SubElement(placemark, "wpml:executeHeight").text = str(config.execute_height)
        ET.SubElement(placemark, "wpml:waypointSpeed").text = str(config.auto_flight_speed)

        heading = ET.SubElement(placemark, "wpml:waypointHeadingParam")
        ET.SubElement(heading, "wpml:waypointHeadingMode").text = config.waypoint_heading_mode
        ET.SubElement(heading, "wpml:waypointHeadingAngle").text = str(
            config.waypoint_heading_angle if config.waypoint_heading_angle is not None else 0
        )
        ET.SubElement(heading, "wpml:waypointPoiPoint").text = "0.000000,0.000000,0.000000"
        ET.SubElement(heading, "wpml:waypointHeadingAngleEnable").text = (
            "1" if config.waypoint_heading_angle is not None else "0"
        )
        ET.SubElement(heading, "wpml:waypointHeadingPathMode").text = "followBadArc"
        ET.SubElement(heading, "wpml:waypointHeadingPoiIndex").text = "0"

        turn = ET.SubElement(placemark, "wpml:waypointTurnParam")
        ET.SubElement(turn, "wpml:waypointTurnMode").text = "toPointAndStopWithDiscontinuityCurvature"
        ET.SubElement(turn, "wpml:waypointTurnDampingDist").text = "0"

        ET.SubElement(placemark, "wpml:useStraightLine").text = "1"

        action_group = ET.SubElement(placemark, "wpml:actionGroup")
        ET.SubElement(action_group, "wpml:actionGroupId").text = index_text
        ET.SubElement(action_group, "wpml:actionGroupStartIndex").text = index_text
        ET.SubElement(action_group, "wpml:actionGroupEndIndex").text = index_text
        ET.SubElement(action_group, "wpml:actionGroupMode").text = "sequence"

        trigger = ET.SubElement(action_group, "wpml:actionTrigger")
        ET.SubElement(trigger, "wpml:actionTriggerType").text = "reachPoint"

        action_id = -1
        if pitch is not None:
            action_id += 1
            action = ET.SubElement(action_group, "wpml:action")
            ET.SubElement(action, "wpml:actionId").text = str(action_id)
            ET.SubElement(action, "wpml:actionActuatorFunc").text = "gimbalRotate"
            param = ET.SubElement(action, "wpml:actionActuatorFuncParam")
            ET.SubElement(param, "wpml:gimbalHeadingYawBase").text = "north"
            ET.SubElement(param, "wpml:gimbalRotateMode").text = "absoluteAngle"
            ET.SubElement(param, "wpml:gimbalPitchRotateEnable").text = "1"
            ET.SubElement(param, "wpml:gimbalPitchRotateAngle").text = str(pitch)
            ET.SubElement(param, "wpml:gimbalRollRotateEnable").text = "0"
            ET.SubElement(param, "wpml:gimbalRollRotateAngle").text = "0"
            ET.SubElement(param, "wpml:gimbalYawRotateEnable").text = "0"
            ET.SubElement(param, "wpml:gimbalYawRotateAngle").text = "0"
            ET.SubElement(param, "wpml:gimbalRotateTimeEnable").text = "0"
            ET.SubElement(param, "wpml:gimbalRotateTime").text = "0"
            ET.SubElement(param, "wpml:payloadPositionIndex").text = str(payload_pos)

        if yaw is not None:
            action_id += 1
            action = ET.SubElement(action_group, "wpml:action")
            ET.SubElement(action, "wpml:actionId").text = str(action_id)
            ET.SubElement(action, "wpml:actionActuatorFunc").text = "gimbalRotate"
            param = ET.SubElement(action, "wpml:actionActuatorFuncParam")
            ET.SubElement(param, "wpml:gimbalHeadingYawBase").text = "north"
            ET.SubElement(param, "wpml:gimbalRotateMode").text = "absoluteAngle"
            ET.SubElement(param, "wpml:gimbalPitchRotateEnable").text = "0"
            ET.SubElement(param, "wpml:gimbalPitchRotateAngle").text = "0"
            ET.SubElement(param, "wpml:gimbalRollRotateEnable").text = "0"
            ET.SubElement(param, "wpml:gimbalRollRotateAngle").text = "0"
            ET.SubElement(param, "wpml:gimbalYawRotateEnable").text = "1"
            ET.SubElement(param, "wpml:gimbalYawRotateAngle").text = str(yaw)
            ET.SubElement(param, "wpml:gimbalRotateTimeEnable").text = "0"
            ET.SubElement(param, "wpml:gimbalRotateTime").text = "0"
            ET.SubElement(param, "wpml:payloadPositionIndex").text = str(payload_pos)

        action_id += 1
        photo_action = ET.SubElement(action_group, "wpml:action")
        ET.SubElement(photo_action, "wpml:actionId").text = str(action_id)
        ET.SubElement(photo_action, "wpml:actionActuatorFunc").text = "takePhoto"
        photo_param = ET.SubElement(photo_action, "wpml:actionActuatorFuncParam")
        ET.SubElement(photo_param, "wpml:fileSuffix").text = f"航点{idx + 1}"
        ET.SubElement(photo_param, "wpml:payloadPositionIndex").text = str(payload_pos)
        ET.SubElement(photo_param, "wpml:useGlobalPayloadLensIndex").text = (
            "1" if config.drone_type == "M3T" else "0"
        )

        if config.drone_type != "M3T":
            gimbal_heading = ET.SubElement(placemark, "wpml:waypointGimbalHeadingParam")
            ET.SubElement(gimbal_heading, "wpml:waypointGimbalPitchAngle").text = "0"
            ET.SubElement(gimbal_heading, "wpml:waypointGimbalHeadingAngle").text = "0"
            ET.SubElement(placemark, "wpml:isRisky").text = "0"
            ET.SubElement(placemark, "wpml:waypointWorkType").text = "0"

    return wpml


def export_waypoints_to_kmz(
    lon_lat_points: Sequence[Tuple[float, float]],
    output_kmz_path: str,
    config: MissionConfig,
    pitch: Optional[float] = None,
    yaw: Optional[float] = None,
) -> str:
    if len(lon_lat_points) == 0:
        raise ValueError("没有可导出的航点")

    _validate_export_config(config, pitch, yaw)

    output_path = Path(output_kmz_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    template_kml = _build_template_kml(lon_lat_points, config, pitch, yaw)
    waylines_wpml = _build_waylines_wpml(lon_lat_points, config, pitch, yaw)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as kmz:
        kmz.writestr("wpmz/template.kml", _pretty_xml(template_kml))
        kmz.writestr("wpmz/waylines.wpml", _pretty_xml(waylines_wpml))

    return str(output_path)
