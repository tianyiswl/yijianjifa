#define MyAppName "一剪即发"
#define MyAppVersion "0.1.42"
#define MyAppExeName "一剪即发.exe"

[Setup]
AppId={{2D07F654-3F56-4BF4-BA3D-8A3D303BE724}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\一剪即发
DefaultGroupName=一剪即发
OutputDir=..\..\dist\installer
OutputBaseFilename=YiJianJiFa-Windows-x64-Setup
SetupIconFile=..\resources\icons\yijianjifa.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest

[Files]
Source: "..\..\dist\一剪即发\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs


[Icons]
Name: "{autoprograms}\一剪即发"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\一剪即发"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动一剪即发"; Flags: nowait postinstall skipifsilent
