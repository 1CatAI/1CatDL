/**
 * Scope cancellation to the whole request, including response-body reads.
 * Uses AbortController rather than newer AbortSignal static methods so the
 * customer portal also works in browsers that do not expose those methods.
 *
 * @template T
 * @param {(signal: AbortSignal) => Promise<T>} operation
 * @param {number} timeoutMs
 * @param {AbortSignal | null} [parent]
 * @returns {Promise<T>}
 */
export async function withRequestTimeout(operation, timeoutMs, parent) {
  const controller = new AbortController();
  const timeoutError = new Error('请求超时，请检查网络后重试。');
  timeoutError.name = 'TimeoutError';
  const cancelledError = new Error('请求已取消');
  cancelledError.name = 'AbortError';
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort(timeoutError);
  }, timeoutMs);
  const cancel = () => {
    clearTimeout(timer);
    if (!controller.signal.aborted) controller.abort(parent?.reason);
  };
  parent?.addEventListener('abort', cancel, { once: true });
  try {
    if (parent?.aborted) cancel();
    if (controller.signal.aborted) throw parent?.reason ?? cancelledError;
    const result = await operation(controller.signal);
    // JSON fallbacks must not turn an aborted body read into a successful {}.
    if (timedOut) throw timeoutError;
    if (controller.signal.aborted) throw parent?.reason ?? cancelledError;
    return result;
  } catch (error) {
    // Older browsers ignore abort(reason), so normalize timeouts ourselves.
    if (timedOut) throw timeoutError;
    throw error;
  } finally {
    clearTimeout(timer);
    parent?.removeEventListener('abort', cancel);
  }
}
