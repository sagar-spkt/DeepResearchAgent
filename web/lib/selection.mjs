// A ticket belongs to one selection request, including repeated loads of the same ID.
export function createSelectionGate() {
  let generation = 0;
  let current = Object.freeze({id: null, generation});
  return {
    begin(id) {current = Object.freeze({id, generation: ++generation}); return current;},
    snapshot() {return current;},
    isCurrent(ticket) {return ticket === current;},
  };
}
export async function commitIfCurrent(gate, ticket, load, commit) {
  const value = await load();
  if (!gate.isCurrent(ticket)) return null;
  commit(value);
  return value;
}
export function documentUsage(previous = [], received = []) {
  return [...new Set([...previous, ...received].filter(id => typeof id === 'string'))];
}
