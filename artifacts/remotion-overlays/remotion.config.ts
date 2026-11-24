import { Config } from "@remotion/cli/config";

// Transparent overlays, same job motion.py does with VP9 yuva420p.
Config.setVideoImageFormat("png");
Config.setPixelFormat("yuva420p");
Config.setCodec("vp8");
