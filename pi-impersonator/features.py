PROTOCOL_VERSION = '4.4'
FIRMWARE_VERSION = '3.1.6'
MODEL = 'Buddy3D-C1'
MANUFACTURER = 'Niceboy'
HW_VERSION = 'NB.1.1.0'
USER_AGENT = 'Buddy3D Camera'
TRIGGER_SCHEME = 'THIRTY_SEC'

# GAP-CAP-01 / GAP-DEVICE-02: advertise only features the Pi can honor. IrMode,
# SpeakerVolume and FanControl were removed (no such hardware). MicroSd is kept
# because the Pi backs it with an emulated SD at /mnt/sdcard (see timelapse.py
# and the SMB share) so Connect's timelapse UI works.
FEATURES = (
    '"SocketCom","UploadInterval","TimelapseEn","TimelapseInterval",'
    '"TimelapseVideoMake","TimelapseFileList","VideoStream","RtspStream",'
    '"GetSnapshot","WiFi","FwVer","HwVer",'
    '"CameraName","MicroSd","FwUpdate","CameraReboot","McuTemp",'
    '"VideoQuality","WebRtc","TurnVideoQualityChange","trigger_scheme"'
)

FEATURES_LIST = [
    'SocketCom','UploadInterval','TimelapseEn','TimelapseInterval','TimelapseVideoMake',
    'TimelapseFileList','VideoStream','RtspStream','GetSnapshot',
    'WiFi','FwVer','HwVer','CameraName','MicroSd','FwUpdate','CameraReboot','McuTemp',
    'VideoQuality','WebRtc','TurnVideoQualityChange','trigger_scheme'
]
