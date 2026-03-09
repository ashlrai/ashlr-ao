import {Composition} from 'remotion';
import {Video} from './Video';

export const Root: React.FC = () => {
  return (
    <Composition
      id="AshlrDemo"
      component={Video}
      durationInFrames={900}
      fps={30}
      width={1920}
      height={1080}
    />
  );
};
