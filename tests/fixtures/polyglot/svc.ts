export class Service {
  run(): number {
    return 1;
  }
}

export function run(): number {
  return new Service().run();
}
