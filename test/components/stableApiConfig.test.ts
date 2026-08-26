import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

/**
 * sameOriginApiConfig() returns a fresh object on every call. Bound to a variable
 * without useMemo, that variable changes identity every render — so every
 * useCallback depending on it changes too, and every effect depending on THOSE
 * re-fires: fetch -> setState -> re-render -> fetch.
 *
 * useTenantAdmin shipped without the useMemo and looped at roughly 5-11 requests
 * per second. Admin calls proxy through a per-invocation-billed function, so the
 * loop exhausted a month of hosting credits twice in two days and took staging
 * offline. lib/api.ts already keeps useApiClient stable for this exact reason and
 * explains why in a comment — which did not prevent it happening one file over.
 *
 * A comment is not a constraint. This is.
 */
function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === '.next' || entry.startsWith('.')) continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (/\.tsx?$/.test(full)) out.push(full)
  }
  return out
}

const BIND = /(const|let|var)\s+\w+\s*=\s*sameOriginApiConfig\(\)/

describe('sameOriginApiConfig must never be bound unmemoized', () => {
  const files = ['app', 'components', 'lib'].flatMap((r) => walk(r))

  it('finds the call sites at all, so the test cannot pass by finding nothing', () => {
    const callers = files.filter((f) => readFileSync(f, 'utf8').includes('sameOriginApiConfig()'))
    expect(callers.length).toBeGreaterThan(3)
  })

  it('wraps every BOUND use in useMemo', () => {
    // Inline use — api.get(url, sameOriginApiConfig()) — is fine: the object is
    // consumed immediately and never reaches a dependency array. Only a binding is
    // dangerous, because the binding is what a useCallback depends on.
    const offenders: string[] = []
    for (const file of files) {
      if (file.includes('api.ts')) continue
      readFileSync(file, 'utf8')
        .split('\n')
        .forEach((line, i) => {
          if (!BIND.test(line)) return
          if (line.includes('useMemo')) return
          offenders.push(`${file}:${i + 1} ${line.trim()}`)
        })
    }
    expect(offenders, `bound without useMemo:\n${offenders.join('\n')}`).toEqual([])
  })
})
