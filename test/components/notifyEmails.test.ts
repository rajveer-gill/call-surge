import { describe, it, expect } from 'vitest'
import { splitEmails, joinEmails } from '@/lib/notifyEmails'

// Settings edits "where booking requests get emailed" as rows with add/remove buttons,
// but the business record stores one comma-separated string that the backend splits when
// a request comes in. These two functions are that boundary, so an address a shop saved
// before the UI changed has to survive a round trip untouched.

describe('splitEmails', () => {
  it('turns a stored string into rows', () => {
    expect(splitEmails('frontdesk@salon.com, manager@salon.com')).toEqual([
      'frontdesk@salon.com',
      'manager@salon.com',
    ])
  })

  it('always yields at least one row, so the field is never empty', () => {
    for (const raw of ['', '   ', undefined, null, ',,', ' ; ']) {
      expect(splitEmails(raw)).toEqual([''])
    }
  })

  it('tolerates semicolons and stray spacing, because people paste from Outlook', () => {
    expect(splitEmails(' a@b.com ;c@d.com,  e@f.com ')).toEqual([
      'a@b.com',
      'c@d.com',
      'e@f.com',
    ])
  })
})

describe('joinEmails', () => {
  it('turns rows back into the stored string', () => {
    expect(joinEmails(['frontdesk@salon.com', 'manager@salon.com'])).toBe(
      'frontdesk@salon.com, manager@salon.com',
    )
  })

  it('drops blank rows — an untouched "Add email" row is not an address', () => {
    expect(joinEmails(['a@b.com', '', '   ', 'c@d.com'])).toBe('a@b.com, c@d.com')
  })

  it('collapses duplicates so nobody is emailed twice about one booking', () => {
    expect(joinEmails(['a@b.com', 'A@B.com', ' a@b.com '])).toBe('a@b.com')
  })

  it('returns empty when every row is blank — this is how a shop turns emails off', () => {
    expect(joinEmails([''])).toBe('')
    expect(joinEmails(['  ', ''])).toBe('')
  })
})

describe('round trip', () => {
  it('leaves an already-saved value unchanged', () => {
    const stored = 'frontdesk@salon.com, manager@salon.com'
    expect(joinEmails(splitEmails(stored))).toBe(stored)
  })

  it('normalises a messy stored value the first time it is opened and saved', () => {
    expect(joinEmails(splitEmails('a@b.com;;  c@d.com , '))).toBe('a@b.com, c@d.com')
  })

  it('an untouched empty field stays empty rather than becoming a stray comma', () => {
    expect(joinEmails(splitEmails(''))).toBe('')
  })
})
