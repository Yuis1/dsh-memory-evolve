/** Reuse DSH's authority fence, signed credentials and live Peer ownership.
 * Every plugin HTTP surface operates on this owner's files/configuration.
 * Missing/older Connection services cannot safely serve these routes.
 */
export function registerOwnerRoute(ctx, route) {
  return ctx.webServer.register({
    ...route,
    async handler(req, res) {
      let admission
      try {
        const connection = ctx.get('connection')
        if (typeof connection?.admit !== 'function') throw new Error('Connection unavailable')
        admission = connection.admit(req)
      } catch {
        res.writeHead(503, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ error: 'Native DSH authentication unavailable' }))
        return
      }
      const status = admission.rejection ?? (admission.peer?.principal?.kind === 'owner' && admission.peer.signal?.aborted === false ? undefined : 403)
      if (status !== undefined) {
        res.writeHead(status, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ error: status === 401 ? 'Authentication required' : 'Owner access required' }))
        return
      }
      await route.handler(req, res)
    },
  })
}

/** Models request a one-action grant from DSH, never from tool arguments. */
export async function approveExecution(ctx, exec, toolName, reason) {
  try {
    const approval = ctx.get('approval')
    if (!approval || !exec?.agent) return false
    return await approval.request({ agent: exec.agent, toolName, callId: exec.callId, signal: exec.signal, reason }) === 'allowed-once'
  } catch {
    return false
  }
}
