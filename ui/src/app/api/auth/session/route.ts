import { cookies } from 'next/headers';
import { NextRequest, NextResponse } from 'next/server';

const OSS_TOKEN_COOKIE = 'dograh_auth_token';
const OSS_USER_COOKIE = 'dograh_auth_user';

export async function POST(request: NextRequest) {
  const { token, user } = await request.json();

  if (!token) {
    return NextResponse.json({ error: 'Missing token' }, { status: 400 });
  }

  // process.env.NODE_ENV === 'production' is baked in at `next build` time
  // by webpack's DefinePlugin and can't be overridden by a runtime env var
  // (see ui/Dockerfile's ENV NODE_ENV=production before the build step) —
  // it was always true regardless of deployment mode, so Secure cookies got
  // set even for plain-HTTP access (breaking login there, since browsers
  // silently drop Secure cookies over HTTP). Derive it from the actual
  // request instead: X-Forwarded-Proto (set by Cloudflare's tunnel/edge,
  // which terminates TLS and forwards to this origin over plain HTTP) when
  // present, otherwise the request's own scheme for direct/unproxied access.
  const forwardedProto = request.headers.get('x-forwarded-proto');
  const isHttps = (forwardedProto ?? new URL(request.url).protocol.replace(':', '')) === 'https';

  const cookieStore = await cookies();

  cookieStore.set(OSS_TOKEN_COOKIE, token, {
    httpOnly: true,
    secure: isHttps,
    sameSite: 'lax',
    maxAge: 60 * 60 * 24 * 30,
    path: '/',
  });

  cookieStore.set(OSS_USER_COOKIE, JSON.stringify(user), {
    httpOnly: true,
    secure: isHttps,
    sameSite: 'lax',
    maxAge: 60 * 60 * 24 * 30,
    path: '/',
  });

  return NextResponse.json({ success: true });
}
