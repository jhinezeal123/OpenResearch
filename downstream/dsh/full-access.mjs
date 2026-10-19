// OpenResearch's DSH adapter is explicitly configured for full access.
// Apply the native preset on creation AND resume: environment defaults alone
// cannot change permissions already persisted in an existing DSH session.
export const name = 'orx-dsh-full-access';
export const inject = ['permissionPresets'];
export function apply(ctx) {
  ctx.on('agent/created', ({ agent }) => {
    ctx.permissionPresets.set(agent.session, 'danger-full-access');
  });
}
