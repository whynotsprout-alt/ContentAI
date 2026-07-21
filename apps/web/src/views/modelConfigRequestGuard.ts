export type ModelConfigDraft = {
  baseUrl: string;
  apiKey: string;
  modelName: string;
};

export type ModelConfigRequestTicket = Readonly<{
  generation: number;
  signature: string;
}>;

function normalizedSignature(draft: ModelConfigDraft): string {
  return JSON.stringify([
    draft.baseUrl.trim(),
    draft.apiKey,
    draft.modelName.trim()
  ]);
}

export function createModelConfigRequestGuard(readDraft: () => ModelConfigDraft) {
  let generation = 0;

  return {
    begin(): ModelConfigRequestTicket {
      generation += 1;
      return { generation, signature: normalizedSignature(readDraft()) };
    },
    invalidate(): void {
      generation += 1;
    },
    isLatest(ticket: ModelConfigRequestTicket): boolean {
      return ticket.generation === generation;
    },
    isCurrent(ticket: ModelConfigRequestTicket): boolean {
      return ticket.generation === generation
        && ticket.signature === normalizedSignature(readDraft());
    }
  };
}
