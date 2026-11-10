// Existing component suites exercise owner-authorized behavior. Authentication
// itself is verified separately against the real native carrier in the E2E.
const owner = { principal: { kind: 'owner' }, signal: new AbortController().signal }
const connection = { admit: () => ({ peer: owner }) }
const approval = { request: async () => 'allowed-once' }
export function authorizedContext(ctx = {}) {
  return new Proxy(ctx, {
    get(target, key) {
      if (key === 'get') return (name) => name === 'connection' ? connection : name === 'approval' ? (target.get?.(name) ?? approval) : target.get?.(name)
      if (key === 'inject' && target.inject) return (names, cb) => target.inject(names, (child) => cb(authorizedContext(child)))
      return Reflect.get(target, key)
    },
  })
}
export function authorizedInstaller(fn) {
  return (ctx, ...args) => fn(authorizedContext(ctx), ...args)
}
