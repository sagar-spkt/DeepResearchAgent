export function canViewAdminNavigation(identity) {
  return Boolean(identity?.approved && ['owner', 'admin'].includes(identity.role));
}
