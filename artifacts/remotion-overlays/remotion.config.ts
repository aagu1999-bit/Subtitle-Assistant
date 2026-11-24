import { Config } from "@remotion/cli/config";

// Transparent overlays, same job motion.py does with VP9 yuva420p.
Config.setVideoImageFormat("png");
Config.setPixelFormat("yuva420p");
Config.setCodec("vp8");
// Replit's Docker cap is below Remotion's default ~4GB guess.
Config.setConcurrency(1);

// Official video building blocks (charts, maps, captions, lower thirds…).
// Browse in Studio → Actions → Browse Elements. Copies source when you add one.
Config.addElementLibrary({
  url: "https://www.remotion.dev/elements",
  displayName: "Remotion Elements",
});

