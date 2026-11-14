#ifndef PayloadDir
  #error PayloadDir must point at the validated onedir package
#endif
#ifndef ReleaseDir
  #error ReleaseDir must point at the release output directory
#endif
#ifndef ChineseLanguage
  #error ChineseLanguage must point at the verified Chinese translation
#endif
#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif

[Setup]
AppId={{754D3BD6-BAF9-468F-AB55-D88BC479CE1B}
AppName=鲸伴
AppVersion={#AppVersion}
AppVerName=鲸伴
AppPublisher=鲸伴
DefaultDirName={localappdata}\Programs\Jingban
DefaultGroupName=鲸伴
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
OutputDir={#ReleaseDir}
OutputBaseFilename=鲸伴安装
SetupIconFile=..\..\assets\branding\windows\whale-maid.ico
UninstallDisplayName=鲸伴
UninstallDisplayIcon={app}\鲸伴.exe
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName=鲸伴
VersionInfoProductVersion={#AppVersion}.0
VersionInfoProductTextVersion={#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimp"; MessagesFile: "{#ChineseLanguage}"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\鲸伴"; Filename: "{app}\鲸伴.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\鲸伴"; Filename: "{app}\鲸伴.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\鲸伴.exe"; Description: "启动鲸伴"; Flags: nowait postinstall skipifsilent
