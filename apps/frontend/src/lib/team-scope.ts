export function shouldClearTeamOnProjectChange(
  previousProjectId: string | null,
  nextProjectId: string | null,
): boolean {
  return previousProjectId !== null && previousProjectId !== nextProjectId;
}
