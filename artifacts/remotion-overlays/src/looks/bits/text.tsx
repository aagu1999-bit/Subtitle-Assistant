import React from "react";
import { Easing, interpolate, random } from "remotion";
import {
  BitsStage, GOLD, GoldTitle, useClock,
  type BitsCountProps, type BitsTextProps, type BitsWordsProps,
} from "./shared";

const charsOf = (text: string) => Array.from(text);

export const BlurTextLook: React.FC<BitsTextProps> = ({
  text = "WATCH THIS", color = GOLD,
}) => {
  const { frame, fps } = useClock();
  return (
    <BitsStage>
      <GoldTitle color={color} style={{ display: "flex", flexWrap: "wrap", justifyContent: "center" }}>
        {charsOf(text).map((ch, i) => {
          const t0 = i * 2;
          const p = interpolate(frame, [t0, t0 + fps * 0.4], [0, 1], {
            extrapolateLeft: "clamp", extrapolateRight: "clamp",
            easing: Easing.out(Easing.cubic),
          });
          return (
            <span key={i} style={{
              display: "inline-block",
              filter: `blur(${(1 - p) * 14}px)`,
              opacity: 0.2 + p * 0.8,
              transform: `translateY(${(1 - p) * 18}px)`,
            }}>{ch === " " ? "\u00a0" : ch}</span>
          );
        })}
      </GoldTitle>
    </BitsStage>
  );
};

export const CountUpLook: React.FC<BitsCountProps> = ({ to = 10000, color = GOLD }) => {
  const { frame, durationInFrames } = useClock();
  const n = interpolate(frame, [0, durationInFrames * 0.8], [0, to], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });
  return (
    <BitsStage>
      <GoldTitle color={color} style={{ fontSize: 120, fontVariantNumeric: "tabular-nums" }}>
        {Math.round(n).toLocaleString("en-US")}
      </GoldTitle>
    </BitsStage>
  );
};

export const TextTypeLook: React.FC<BitsTextProps> = ({
  text = "WATCH THIS", color = GOLD,
}) => {
  const { frame, fps, durationInFrames } = useClock();
  const typed = Math.floor(interpolate(frame, [6, durationInFrames * 0.7], [0, text.length], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  }));
  const blink = Math.floor(frame / (fps * 0.4)) % 2 === 0;
  return (
    <BitsStage>
      <GoldTitle color={color} style={{ fontFamily: "ui-monospace, Menlo, monospace", fontSize: 64 }}>
        {text.slice(0, typed)}
        <span style={{ opacity: blink ? 1 : 0, color }}>{typed < text.length ? "|" : ""}</span>
      </GoldTitle>
    </BitsStage>
  );
};

export const GradientTextLook: React.FC<BitsTextProps> = ({
  text = "WATCH THIS", color = GOLD,
}) => {
  const { frame } = useClock();
  const pos = (frame * 3) % 200;
  return (
    <BitsStage>
      <GoldTitle style={{
        backgroundImage: `linear-gradient(90deg, ${color}, #00FF88, #00E5FF, ${color})`,
        backgroundSize: "220% 100%",
        backgroundPosition: `${pos - 20}% 50%`,
        WebkitBackgroundClip: "text",
        backgroundClip: "text",
        color: "transparent",
        WebkitTextFillColor: "transparent",
      }}>{text}</GoldTitle>
    </BitsStage>
  );
};

export const GlitchTextLook: React.FC<BitsTextProps> = ({
  text = "WATCH THIS", color = GOLD,
}) => {
  const { frame } = useClock();
  const hit = frame % 14 < 4;
  const jx = hit ? Math.round(Math.sin(frame * 3.1) * 8) : 0;
  const jy = hit ? Math.round(Math.cos(frame * 2.4) * 4) : 0;
  return (
    <BitsStage>
      <div style={{ position: "relative" }}>
        <GoldTitle color="#FF3B6B" style={{
          position: "absolute", left: jx - 4, top: jy, opacity: hit ? 0.85 : 0, mixBlendMode: "screen",
        }}>{text}</GoldTitle>
        <GoldTitle color="#00E5FF" style={{
          position: "absolute", left: -jx + 4, top: -jy, opacity: hit ? 0.85 : 0, mixBlendMode: "screen",
        }}>{text}</GoldTitle>
        <GoldTitle color={color}>{text}</GoldTitle>
      </div>
    </BitsStage>
  );
};

const SCRAMBLE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$#*";

export const ShuffleTextLook: React.FC<BitsTextProps> = ({
  text = "WATCH THIS", color = GOLD,
}) => {
  const { frame, fps } = useClock();
  return (
    <BitsStage>
      <GoldTitle color={color} style={{ fontFamily: "ui-monospace, Menlo, monospace" }}>
        {charsOf(text).map((ch, i) => {
          const locked = frame > i * 3 + fps * 0.35;
          if (ch === " " || locked) return <span key={i}>{ch === " " ? "\u00a0" : ch}</span>;
          const pick = Math.floor(random(`sh-${i}-${Math.floor(frame / 2)}`) * SCRAMBLE.length);
          return <span key={i} style={{ opacity: 0.7 }}>{SCRAMBLE[pick]}</span>;
        })}
      </GoldTitle>
    </BitsStage>
  );
};

export const DecryptedTextLook: React.FC<BitsTextProps> = ({
  text = "WATCH THIS", color = GOLD,
}) => {
  const { frame, fps } = useClock();
  return (
    <BitsStage>
      <GoldTitle color={color} style={{ fontFamily: "ui-monospace, Menlo, monospace", fontSize: 64 }}>
        {charsOf(text).map((ch, i) => {
          const locked = frame > fps * 0.2 + i * 4;
          if (ch === " " || locked) return <span key={i}>{ch === " " ? "\u00a0" : ch}</span>;
          const pick = Math.floor(random(`dc-${i}-${frame}`) * SCRAMBLE.length);
          return <span key={i} style={{ color: "#00FF88" }}>{SCRAMBLE[pick]}</span>;
        })}
      </GoldTitle>
    </BitsStage>
  );
};

export const RotatingTextLook: React.FC<BitsWordsProps> = ({
  words = ["START", "GROW", "WIN"], color = GOLD,
}) => {
  const { frame, durationInFrames } = useClock();
  const n = Math.max(1, words.length);
  const slot = durationInFrames / n;
  const idx = Math.min(n - 1, Math.floor(frame / slot));
  const local = frame - idx * slot;
  const y = interpolate(local, [0, 10, slot - 10, slot], [40, 0, 0, -40], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });
  const op = interpolate(local, [0, 8, slot - 8, slot], [0, 1, 1, 0], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });
  return (
    <BitsStage>
      <GoldTitle color={color} style={{ transform: `translateY(${y}px)`, opacity: op }}>
        {words[idx]}
      </GoldTitle>
    </BitsStage>
  );
};

export const ShinyTextLook: React.FC<BitsTextProps> = ({
  text = "WATCH THIS", color = GOLD,
}) => {
  const { frame, durationInFrames } = useClock();
  const shine = interpolate(frame, [0, durationInFrames], [-40, 140], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });
  return (
    <BitsStage>
      <GoldTitle color={color} style={{
        position: "relative",
        backgroundImage: `linear-gradient(110deg, ${color} 35%, #fff 50%, ${color} 65%)`,
        backgroundSize: "180% 100%",
        backgroundPosition: `${shine}% 50%`,
        WebkitBackgroundClip: "text",
        backgroundClip: "text",
        color: "transparent",
        WebkitTextFillColor: "transparent",
      }}>{text}</GoldTitle>
    </BitsStage>
  );
};
