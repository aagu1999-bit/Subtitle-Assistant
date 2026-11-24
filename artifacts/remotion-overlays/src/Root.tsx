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
      defaultProps={{ text: "WATCH THIS", color: "#FFD60A", duration: 3 }} />
  </>
);
