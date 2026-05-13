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
    pkgs.libGL
    pkgs.glib
    pkgs.zlib
  ];
}
