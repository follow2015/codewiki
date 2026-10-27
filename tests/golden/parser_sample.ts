/** 模块文档（黄金样本：TS 后端改解析行为会让 digest 变化）。 */
import { readFileSync } from "node:fs";

export const MAX = 3;

export class Outer {
  run(x: number): number {
    return helper(x);
  }
}

export class Sub extends Outer {
  run(x: number): number {
    return readFileSync("x").length + MAX;
  }
}

function helper(v: number): number {
  return v + MAX;
}
