export type AdminRequestGeneration = number;

export function createAdminRequestGenerationGuard() {
  let generation = 0;

  return {
    begin(): AdminRequestGeneration {
      generation += 1;
      return generation;
    },
    invalidate(): void {
      generation += 1;
    },
    isCurrent(ticket: AdminRequestGeneration): boolean {
      return ticket === generation;
    }
  };
}
