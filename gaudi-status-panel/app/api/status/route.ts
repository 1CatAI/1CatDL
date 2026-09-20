export const dynamic = 'force-dynamic';

const collectorUrl =
  process.env.GAUDI_COLLECTOR_URL ??
  'http://127.0.0.1:18765/api/status';

export async function GET() {
  try {
    const response = await fetch(collectorUrl, {
      cache: 'no-store',
      signal: AbortSignal.timeout(4000),
    });
    const body = await response.text();

    return new Response(body, {
      status: response.status,
      headers: {
        'Content-Type': 'application/json; charset=utf-8',
        'Cache-Control': 'no-store, max-age=0',
        'X-Content-Type-Options': 'nosniff',
      },
    });
  } catch {
    return Response.json(
      {
        status: 'offline',
        message: 'Gaudi telemetry collector is unavailable',
      },
      {
        status: 503,
        headers: {
          'Cache-Control': 'no-store, max-age=0',
          'X-Content-Type-Options': 'nosniff',
        },
      },
    );
  }
}
