import React from "react";
import { AbsoluteFill } from "remotion";

/** Host layer; `remotion elements use` drops the official Element in here. */
export const BlankLook: React.FC = () => (
  <AbsoluteFill style={{ background: "transparent" }} />
);
