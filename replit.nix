{pkgs}: {
  deps = [
    pkgs.freetype
    pkgs.fontconfig
    pkgs.ffmpeg-full
    # Native libs OpenCV / MediaPipe link against at runtime. Without these,
    # `import cv2` fails with libxcb.so.1 / libGL.so.1 / libgthread errors
    # on Replit's minimal Nix sandbox — which kills the interview-reframe
    # feature even when pip-install succeeds.
    pkgs.xorg.libxcb
    pkgs.xorg.libX11
    pkgs.xorg.libXext
    pkgs.xorg.libXrender
    pkgs.xorg.libXcomposite
    pkgs.xorg.libXdamage
    pkgs.xorg.libXfixes
    pkgs.xorg.libXrandr
    pkgs.libGL
    pkgs.glib
    pkgs.zlib
    # Playwright Chromium (Capture SERP) — common shared libs on Replit
    pkgs.nss
    pkgs.nspr
    pkgs.atk
    pkgs.at-spi2-atk
    pkgs.cups
    pkgs.dbus
    pkgs.libdrm
    pkgs.libxkbcommon
    pkgs.pango
    pkgs.cairo
    pkgs.alsa-lib
    pkgs.mesa
    pkgs.expat
    pkgs.gtk3
  ];
}
