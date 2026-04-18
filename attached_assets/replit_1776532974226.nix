{ pkgs }: {
  deps = [
    pkgs.ffmpeg-full
    pkgs.python311
    pkgs.python311Packages.pip
    pkgs.fontconfig
    pkgs.freetype
  ];
  env = {
    LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
      pkgs.stdenv.cc.cc.lib
      pkgs.fontconfig.lib
      pkgs.freetype
    ];
  };
}
