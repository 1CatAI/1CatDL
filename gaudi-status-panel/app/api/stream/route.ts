export const dynamic = 'force-dynamic';

const streamUrl =
  process.env.GAUDI_STREAM_URL ??
  'http://127.0.0.1:18765/api/stream';

export async function GET(request: Request) {
  try {
    const response = await fetch(streamUrl, {
      cache: 'no-store',
      headers: {
        Accept: 'text/event-stream',
      },
      signal: request.signal,
    });

    if (!response.ok || !response.body) {
      return Response.json(
        { status: 'offline', message: 'Gaudi telemetry stream is unavailable' },
        { status: 503, headers: { 'Cache-Control': 'no-store, max-age=0' } },
      );
    }

    return new Response(response.body, {
      status: 200,
      headers: {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache, no-transform',
        Connection: 'keep-alive',
        'X-Accel-Buffering': 'no',
        'X-Content-Type-Options': 'nosniff',
      },
    });
  } catch {
    return Response.json(
      { status: 'offline', message: 'Gaudi telemetry stream is unavailable' },
      { status: 503, headers: { 'Cache-Control': 'no-store, max-age=0' } },
    );
  }
}
