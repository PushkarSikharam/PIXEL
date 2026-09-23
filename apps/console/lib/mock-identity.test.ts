import { describe, expect, it } from "vitest";
import { demoCodeFor, invitationStatus, MAX_ATTEMPTS, startChallenge, verifyChallenge, type Challenge } from "./mock-identity";

const now = 1_700_000_000_000;

describe("mock email-code sign-in", () => {
  it("answers every valid address the same way", () => {
    const invited = startChallenge("avery@northwind.example", now) as Challenge;
    const stranger = startChallenge("stranger@elsewhere.example", now) as Challenge;
    expect(Object.keys(invited)).toEqual(Object.keys(stranger));
    expect(invited.expiresAt).toBe(stranger.expiresAt);
    expect(startChallenge("not-an-address", now)).toBe("invalid_email");
  });

  it("a correct code for a non-invited address grants no access", () => {
    const challenge = startChallenge("stranger@elsewhere.example", now) as Challenge;
    expect(verifyChallenge(challenge, demoCodeFor(challenge.email), now).result).toBe("no_access");
  });

  it("codes are single use, expire, and lock after too many attempts", () => {
    const challenge = startChallenge("avery@northwind.example", now) as Challenge;
    const code = demoCodeFor(challenge.email);
    const ok = verifyChallenge(challenge, code, now);
    expect(ok.result).toBe("ok");
    expect(verifyChallenge(ok.challenge, code, now).result).toBe("code_invalid");
    expect(verifyChallenge(challenge, code, now + 10 * 60_000).result).toBe("code_expired");
    let current = challenge;
    for (let i = 0; i < MAX_ATTEMPTS; i += 1) current = verifyChallenge(current, "999999x", now).challenge;
    expect(verifyChallenge(current, code, now).result).toBe("too_many_attempts");
  });
});

describe("mock invitations", () => {
  it("covers every acceptance state", () => {
    expect(invitationStatus("demo-valid", null)).toBe("valid");
    expect(invitationStatus("demo-valid", "farah@northwind.example")).toBe("valid");
    expect(invitationStatus("demo-valid", "mallory@evil.example")).toBe("wrong_account");
    expect(invitationStatus("demo-expired", null)).toBe("expired");
    expect(invitationStatus("demo-used", null)).toBe("used");
    expect(invitationStatus("demo-revoked", null)).toBe("revoked");
    expect(invitationStatus("made-up", null)).toBe("invalid");
  });
});
