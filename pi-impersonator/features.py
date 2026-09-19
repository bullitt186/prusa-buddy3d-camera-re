PROTOCOL_VERSION = '4.4'
FIRMWARE_VERSION = '3.1.6'
MODEL = 'Buddy3D-C1'
MANUFACTURER = 'Niceboy'
HW_VERSION = 'NB.1.1.0'
USER_AGENT = 'Buddy3D Camera'
TRIGGER_SCHEME = 'THIRTY_SEC'

# GAP-CAP-01 / GAP-DEVICE-02: only advertise features the Pi impersonator can
# actually honor. The real Buddy3D-C1 has an IR illuminator, speaker, fan and
# MicroSD; the Pi has none, so advertising IrMode/SpeakerVolume/FanControl/MicroSd
# made Connect show controls that could never work. Removed those four.
FEATURES = (
    '"SocketCom","UploadInterval","TimelapseEn","TimelapseInterval",'
    '"TimelapseVideoMake","TimelapseFileList","VideoStream","RtspStream",'
    '"GetSnapshot","WiFi","FwVer","HwVer",'
    '"CameraName","FwUpdate","CameraReboot","McuTemp",'
    '"VideoQuality","WebRtc","TurnVideoQualityChange","trigger_scheme"'
)

FEATURES_LIST = [
    'SocketCom','UploadInterval','TimelapseEn','TimelapseInterval','TimelapseVideoMake',
    'TimelapseFileList','VideoStream','RtspStream','GetSnapshot',
    'WiFi','FwVer','HwVer','CameraName','FwUpdate','CameraReboot','McuTemp',
    'VideoQuality','WebRtc','TurnVideoQualityChange','trigger_scheme'
]
