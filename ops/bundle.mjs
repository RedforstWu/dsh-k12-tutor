import * as board from '../packages/k12-whiteboard/src/index.mjs';
import * as experience from '../packages/k12-experience/src/index.mjs';

export const name = 'dsh-k12-tutor';
export function apply(ctx, config = {}) {
  ctx.plugin(board, config);
  ctx.plugin(experience, config);
}
