import {AbsoluteFill, Sequence} from 'remotion';
import {Intro} from './scenes/Intro';
import {Problem} from './scenes/Problem';
import {Solution} from './scenes/Solution';
import {Features} from './scenes/Features';
import {ForSale} from './scenes/ForSale';
import {theme} from './lib/theme';

export const SaleVideo: React.FC = () => {
  return (
    <AbsoluteFill style={{backgroundColor: theme.bg}}>
      {/* 0-4s: Intro */}
      <Sequence from={0} durationInFrames={120}>
        <Intro />
      </Sequence>

      {/* 4-8s: Problem */}
      <Sequence from={120} durationInFrames={120}>
        <Problem />
      </Sequence>

      {/* 8-14s: Solution */}
      <Sequence from={240} durationInFrames={180}>
        <Solution />
      </Sequence>

      {/* 14-22s: Features */}
      <Sequence from={420} durationInFrames={240}>
        <Features />
      </Sequence>

      {/* 22-28s: For Sale closing */}
      <Sequence from={660} durationInFrames={180}>
        <ForSale />
      </Sequence>
    </AbsoluteFill>
  );
};
