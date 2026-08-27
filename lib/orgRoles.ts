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

/**
 * Labels are group-scoped on purpose.
 *
 * "Manager" already means a STORE manager in this product — someone invited to one
 * location from that store's page. Offering plain "Manager" on the group's Team
 * panel read as that, so the group-level option looked missing when it was the one
 * being offered. The stored value is still `manager`; only what people read
 * changed.
 */
export const ORG_ROLE_LABEL: Record<string, string> = {
  owner: 'Owner',
  manager: 'Group admin',
  viewer: 'Viewer',
}

export const ORG_ROLE_BLURB: Record<string, string> = {
  owner: 'Runs the whole group. Only one person can be Owner.',
  manager: 'Full access to every store in the group, and can manage people — but cannot change the Owner.',
  viewer: 'Can see every store in the group, but cannot change anything.',
}

/** Shown once beside the role picker, so the limits are stated before choosing. */
export const ORG_ROLE_PICKER_HINT =
  'Group admins get every store in the group. To give someone one store only, invite them from that store instead.'
