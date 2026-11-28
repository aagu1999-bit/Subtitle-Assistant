import { TOKENS } from "./tokens";
import React from "react";
import { Composition } from "remotion";
import { DEFAULT_ORBIT_PROPS, OrbitCarousel, type OrbitProps } from "./OrbitCarousel";
import { NumberCounterLook } from "./looks/NumberCounter";
import { VerticalBarChartLook } from "./looks/VerticalBarChart";
import { LineChartLook } from "./looks/LineChart";
import { WordHighlightCaptionsLook } from "./looks/WordHighlightCaptions";
import { PoppingWordCaptionsLook } from "./looks/PoppingWordCaptions";
import { NameLowerThirdLook } from "./looks/NameLowerThird";
import { YoutubeCommentHighlightLook } from "./looks/YoutubeCommentHighlight";
import { YoutubeSubscribeNudgeLook } from "./looks/YoutubeSubscribeNudge";
import { PolaroidPicturesLook } from "./looks/PolaroidPictures";
import { OnScreenMessagesLook } from "./looks/OnScreenMessages";
import { SplitTextLook } from "./looks/SplitText";
import * as Bits from "./looks/bits";

type DurProps = { duration?: number };

const durationMeta = ({ props }: { props: DurProps }) => ({
  durationInFrames: Math.max(24, Math.round((Number(props.duration) || 4) * 30)),
});

const NINE_BY_SIXTEEN = {
  fps: 30,
  width: 1080,
  height: 1920,
  calculateMetadata: durationMeta,
};

// Studio defaults only -- at render time the colour arrives as a prop
// from _remotion_look_props, which resolves it per project.
const TITLE = { text: "WATCH THIS", color: TOKENS.brandColor, duration: 3 };
const WORDS = { words: ["START", "GROW", "WIN"], color: TOKENS.brandColor, duration: 4 };
const CARDS = { labels: ["One", "Two", "Three"], duration: 4 };

export const RemotionRoot: React.FC = () => (
  <>
    <Composition
      id="OrbitCarousel"
      component={OrbitCarousel}
      durationInFrames={150}
      fps={30}
      width={1080}
      height={420}
      defaultProps={DEFAULT_ORBIT_PROPS}
      calculateMetadata={({ props }: { props: OrbitProps }) => ({
        durationInFrames: Math.max(24, Math.round((props.duration || 5) * 30)),
      })}
    />
    <Composition id="NumberCounter" component={NumberCounterLook} durationInFrames={90}
      defaultProps={{ duration: 3 }} {...NINE_BY_SIXTEEN} />
    <Composition id="VerticalBarChart" component={VerticalBarChartLook} durationInFrames={120}
      defaultProps={{ duration: 4 }} {...NINE_BY_SIXTEEN} />
    <Composition id="LineChart" component={LineChartLook} durationInFrames={120}
      defaultProps={{ duration: 4 }} {...NINE_BY_SIXTEEN} />
    <Composition id="WordHighlightCaptions" component={WordHighlightCaptionsLook} durationInFrames={210}
      defaultProps={{ duration: 7 }} {...NINE_BY_SIXTEEN} />
    <Composition id="PoppingWordCaptions" component={PoppingWordCaptionsLook} durationInFrames={210}
      defaultProps={{ duration: 7 }} {...NINE_BY_SIXTEEN} />
    <Composition id="NameLowerThird" component={NameLowerThirdLook} durationInFrames={90}
      defaultProps={{ duration: 3 }} {...NINE_BY_SIXTEEN} />
    <Composition id="YoutubeCommentHighlight" component={YoutubeCommentHighlightLook} durationInFrames={180}
      defaultProps={{ duration: 6 }} {...NINE_BY_SIXTEEN} />
    <Composition id="YoutubeSubscribeNudge" component={YoutubeSubscribeNudgeLook} durationInFrames={120}
      defaultProps={{ duration: 4 }} {...NINE_BY_SIXTEEN} />
    <Composition id="PolaroidPictures" component={PolaroidPicturesLook} durationInFrames={150}
      defaultProps={{ duration: 5 }} {...NINE_BY_SIXTEEN} />
    <Composition id="OnScreenMessages" component={OnScreenMessagesLook} durationInFrames={162}
      defaultProps={{ duration: 5.4 }} {...NINE_BY_SIXTEEN} />
    <Composition id="SplitText" component={SplitTextLook} durationInFrames={90} {...NINE_BY_SIXTEEN}
      defaultProps={TITLE} />

    <Composition id="BlurText" component={Bits.BlurTextLook} durationInFrames={90} {...NINE_BY_SIXTEEN} defaultProps={TITLE} />
    <Composition id="CountUp" component={Bits.CountUpLook} durationInFrames={90} {...NINE_BY_SIXTEEN}
      defaultProps={{ to: 10000, color: TOKENS.brandColor, duration: 3 }} />
    <Composition id="TextType" component={Bits.TextTypeLook} durationInFrames={90} {...NINE_BY_SIXTEEN} defaultProps={TITLE} />
    <Composition id="GradientText" component={Bits.GradientTextLook} durationInFrames={90} {...NINE_BY_SIXTEEN} defaultProps={TITLE} />
    <Composition id="GlitchText" component={Bits.GlitchTextLook} durationInFrames={90} {...NINE_BY_SIXTEEN} defaultProps={TITLE} />
    <Composition id="ShuffleText" component={Bits.ShuffleTextLook} durationInFrames={90} {...NINE_BY_SIXTEEN} defaultProps={TITLE} />
    <Composition id="DecryptedText" component={Bits.DecryptedTextLook} durationInFrames={90} {...NINE_BY_SIXTEEN} defaultProps={TITLE} />
    <Composition id="RotatingText" component={Bits.RotatingTextLook} durationInFrames={120} {...NINE_BY_SIXTEEN} defaultProps={WORDS} />
    <Composition id="ShinyText" component={Bits.ShinyTextLook} durationInFrames={90} {...NINE_BY_SIXTEEN} defaultProps={TITLE} />

    <Composition id="ProfileCard" component={Bits.ProfileCardLook} durationInFrames={120} {...NINE_BY_SIXTEEN}
      defaultProps={{ text: "ALEX CHEN", duration: 4 }} />
    <Composition id="SpotlightCard" component={Bits.SpotlightCardLook} durationInFrames={120} {...NINE_BY_SIXTEEN}
      defaultProps={{ text: "SPOTLIGHT", duration: 4 }} />
    <Composition id="GlassSurface" component={Bits.GlassSurfaceLook} durationInFrames={120} {...NINE_BY_SIXTEEN}
      defaultProps={{ text: "GLASS", duration: 4 }} />
    <Composition id="TiltedCard" component={Bits.TiltedCardLook} durationInFrames={120} {...NINE_BY_SIXTEEN}
      defaultProps={{ text: "TILT", duration: 4 }} />
    <Composition id="CardSwap" component={Bits.CardSwapLook} durationInFrames={120} {...NINE_BY_SIXTEEN} defaultProps={CARDS} />
    <Composition id="CardStack" component={Bits.CardStackLook} durationInFrames={120} {...NINE_BY_SIXTEEN} defaultProps={CARDS} />
    <Composition id="CircularCarousel" component={Bits.CircularCarouselLook} durationInFrames={150} {...NINE_BY_SIXTEEN} defaultProps={CARDS} />
    <Composition id="BounceCards" component={Bits.BounceCardsLook} durationInFrames={120} {...NINE_BY_SIXTEEN} defaultProps={CARDS} />
    <Composition id="HoloCard" component={Bits.HoloCardLook} durationInFrames={120} {...NINE_BY_SIXTEEN}
      defaultProps={{ text: "HOLO", duration: 4 }} />
    <Composition id="BitsCounter" component={Bits.BitsCounterLook} durationInFrames={90} {...NINE_BY_SIXTEEN}
      defaultProps={{ to: 10000, color: TOKENS.brandColor, duration: 3 }} />

    <Composition id="Aurora" component={Bits.AuroraLook} durationInFrames={150} {...NINE_BY_SIXTEEN} defaultProps={{ duration: 5 }} />
    <Composition id="SoftAurora" component={Bits.SoftAuroraLook} durationInFrames={150} {...NINE_BY_SIXTEEN} defaultProps={{ duration: 5 }} />
    <Composition id="Silk" component={Bits.SilkLook} durationInFrames={150} {...NINE_BY_SIXTEEN} defaultProps={{ duration: 5 }} />
    <Composition id="Beams" component={Bits.BeamsLook} durationInFrames={150} {...NINE_BY_SIXTEEN} defaultProps={{ duration: 5 }} />
    <Composition id="Particles" component={Bits.ParticlesLook} durationInFrames={150} {...NINE_BY_SIXTEEN} defaultProps={{ duration: 5 }} />
    <Composition id="Lightning" component={Bits.LightningLook} durationInFrames={120} {...NINE_BY_SIXTEEN} defaultProps={{ duration: 4 }} />
    <Composition id="LiquidChrome" component={Bits.LiquidChromeLook} durationInFrames={150} {...NINE_BY_SIXTEEN} defaultProps={{ duration: 5 }} />
    <Composition id="Galaxy" component={Bits.GalaxyLook} durationInFrames={150} {...NINE_BY_SIXTEEN} defaultProps={{ duration: 5 }} />
    <Composition id="LightRays" component={Bits.LightRaysLook} durationInFrames={150} {...NINE_BY_SIXTEEN} defaultProps={{ duration: 5 }} />
    <Composition id="Plasma" component={Bits.PlasmaLook} durationInFrames={150} {...NINE_BY_SIXTEEN} defaultProps={{ duration: 5 }} />
    <Composition id="ScreenshotTour" component={Bits.ScreenshotTourLook} durationInFrames={180} {...NINE_BY_SIXTEEN}
      defaultProps={{ caption: "screenshot tour", items: [], duration: 6 }} />
    <Composition id="LooksLikeThis" component={Bits.LooksLikeThisLook} durationInFrames={150} {...NINE_BY_SIXTEEN}
      defaultProps={{ caption: "looks like this", items: [], duration: 5 }} />
  </>
);
