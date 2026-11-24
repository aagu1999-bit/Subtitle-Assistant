import { Config } from "@remotion/cli/config";

// Transparent overlays, same job motion.py does with VP9 yuva420p.
Config.setVideoImageFormat("png");
Config.setPixelFormat("yuva420p");
Config.setCodec("vp8");
// Replit's Docker cap is below Remotion's default ~4GB guess.
Config.setConcurrency(1);

