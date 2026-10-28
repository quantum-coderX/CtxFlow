const SESSION_KEY_PREFIX = "researchos:conversation:";
const fallbackSessions = new Map<string, string>();

export function getConversationSessionId(projectId: string): string {
  const cached = fallbackSessions.get(projectId);
  if (cached) return cached;

  const key = `${SESSION_KEY_PREFIX}${projectId}`;
  try {
    const stored = window.localStorage.getItem(key);
    if (stored && /^[A-Za-z0-9._-]{1,128}$/.test(stored)) {
      fallbackSessions.set(projectId, stored);
      return stored;
    }

    const sessionId = crypto.randomUUID();
    window.localStorage.setItem(key, sessionId);
    fallbackSessions.set(projectId, sessionId);
    return sessionId;
  } catch (error) {
    console.warn("Could not persist the conversation session ID in local storage.", error);
    const sessionId = crypto.randomUUID();
    fallbackSessions.set(projectId, sessionId);
    return sessionId;
  }
}
