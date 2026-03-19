import {Composition} from 'remotion';
import {Video} from './Video';
import {SaleVideo} from './SaleVideo';

export const Root: React.FC = () => {
  return (
    <>
      <Composition
        id="AshlrDemo"
        component={Video}
        durationInFrames={900}
        fps={30}
        width={1920}
        height={1080}
      />
      <Composition
        id="SalePitch"
        component={SaleVideo}
        durationInFrames={840}
        fps={30}
        width={1920}
        height={1080}
      />
    </>
  );
};
