import path from 'node:path';

export async function openExperienceStore(storageDomain) {
  const [{ defineDomain, domainTable }, { z }] = await Promise.all([
    import('@deepseek-ai/dsh-storage-domain'), import('zod'),
  ]);
  const text = z.string().min(1);
  const identifier = text.regex(/^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$/);
  const siteOrigin = text.refine(value => {
    try {
      const url = new URL(value);
      return url.origin === value && (url.protocol === 'https:' ||
        url.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(url.hostname));
    } catch { return false; }
  });
  const request = {
    bindingId: identifier, sessionId: identifier, requestId: identifier,
    payloadHash: text.regex(/^[a-f0-9]{64}$/), credentialFingerprint: text.regex(/^[a-f0-9]{64}$/),
  };
  return storageDomain.open(defineDomain({
    name: 'k12_experience', version: 1,
    tables: {
      bindings: domainTable(z.object({
        siteOrigin, studentId: identifier, studentName: text, currentBookId: text.optional(),
        workspaceId: identifier, workspacePath: text.refine(value => path.isAbsolute(value)),
        createdAt: text.datetime(), updatedAt: text.datetime(), linked: z.boolean(),
      }).strict()),
      requests: domainTable(z.discriminatedUnion('status', [
        z.object({ ...request, status: z.literal('pending') }).strict(),
        z.object({ ...request, status: z.literal('complete'),
          result: z.object({ paperId: identifier, answerLink: text.url() }).strict() }).strict(),
      ])),
    },
  }));
}
