/**
 * Group roles, weakest first. Mirrors ORG_ROLES in backend/database.py — append
 * rather than reorder, since rank depends on position.
 *
 * The reason this is shared rather than compared inline: the stores page tested
 * `role === 'manager'`, so when `owner` was added the head account silently lost
 * the Add store and billing buttons and had FEWER powers on screen than the
 * managers beneath them. An equality check against one role is a bug waiting for
 * the next role.
 */
export const ORG_ROLES = ['viewer', 'manager', 'owner'] as const
export type OrgRole = (typeof ORG_ROLES)[number]

/** Unknown or missing reads as weakest, never strongest — a typo must not grant. */
export function orgRoleRank(role: string | null | undefined): number {
  const i = ORG_ROLES.indexOf((role || '').trim().toLowerCase() as OrgRole)
  return i < 0 ? 0 : i
}

export function orgRoleAtLeast(role: string | null | undefined, minimum: OrgRole): boolean {
  return orgRoleRank(role) >= orgRoleRank(minimum)
}

export const ORG_ROLE_LABEL: Record<string, string> = {
  owner: 'Owner',
  manager: 'Manager',
  viewer: 'Viewer',
}

export const ORG_ROLE_BLURB: Record<string, string> = {
  owner: 'Runs the group. Can hand ownership to someone else.',
  manager: 'Can run the group and manage people, but cannot change the owner.',
  viewer: 'Can see the group, but cannot change anything.',
}
