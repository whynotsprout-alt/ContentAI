export type ModelConfigDraft = {
  baseUrl: string;
  apiKey: string;
  modelName: string;
  temperature?: number | null;
  contextWindowTokens?: number | null;
  chatMaxTokens?: number | null;
  structuredMaxTokens?: number | null;
};

export type ModelConfigRequestTicket = Readonly<{
  generation: number;
  signature: string;
}>;

function normalizedSignature(draft: ModelConfigDraft): string {
  return JSON.stringify([
    draft.baseUrl.trim(),
    draft.apiKey,
    draft.modelName.trim(),
    draft.temperature,
    draft.contextWindowTokens,
    draft.chatMaxTokens,
    draft.structuredMaxTokens
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
