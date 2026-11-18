// 剧创（juchuang）登录：内核换成共享壳（web-shell-kit「认证实现唯一」）。
//
// 原先这份是 103 行残版：只有 code→id_token，**没有 refresh**（会话一过期就得
// 重新登录），也没有静默 SSO。切到壳后两者都自动具备。
//
// 对外形状保持不变（currentToken / isLoggedIn / login / handleCallback / logout），
// App.tsx 与 client.ts 的调用点不动。
//
// 两个本仓特有的点必须保留：
// 1. issuer 传**基地址**（不含 /oidc）：壳拼 `${issuer}/oidc/auth`，传完整 issuer
//    会拼出 /oidc/oidc/auth（实论侧已踩过并被回归用例抓住）。
// 2. 登出仍跳 Logto 的 `/session/end`（壳的 logoutLocal 只清本地会话）。
import { createAuth } from '../kit/auth/index'
import { ssoHintMark as mark, ssoHintSeen as seen } from '../kit/auth/sso-hint'

const ISSUER_BASE = 'https://auth.finddatatech.cloud'
const CLIENT_ID = 'xlp6cjguc16zkxz7hc3d9' // Logto: juchuang-workbench（SPA）
const SCOPE = 'openid profile offline_access'
const TOKEN_KEY = 'juchuang_token'

export const SSO_HINT_DOMAIN = '.finddatatech.cloud'

/** 惰性构造：回跳地址依赖 window.location.origin，模块加载期读它会让本文件在 node 下抛。 */
let instance: ReturnType<typeof createAuth> | null = null
function auth() {
  if (!instance) {
    instance = createAuth({
      fetchConfig: async () => ({
        configured: true,
        issuer: ISSUER_BASE,
        client_id: CLIENT_ID,
        scopes: SCOPE.split(' '),
        organization_required: false,
        missing: [],
      }),
      redirectUri: `${window.location.origin}/workbench`,
      storagePrefix: 'juchuang',
      // 本仓历史口径是 localStorage（client.ts 读的就是它），保持不动。
      tokenStorage: 'local',
    })
  }
  return instance
}

/** 共享壳实例（供 App 的静默 SSO 挂载点使用）。 */
export function authShell() {
  return auth()
}

/** 宿主的 token 槽：壳的会话是唯一真源，这里同步到 client.ts 的读取键。 */
function syncToken(): string | null {
  const token = auth().loadSession()?.idToken ?? null
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
  return token
}

export function currentToken(): string | null {
  return auth().loadSession()?.idToken ?? localStorage.getItem(TOKEN_KEY)
}

export function isLoggedIn(): boolean {
  return Boolean(currentToken())
}

/** 发起登录；未配置时报错而非假登录（壳内的如实阻塞）。 */
export async function login(): Promise<void> {
  await auth().beginLogin()
}

/** 处理回跳：成功返回 true，并把 token 同步到 client 的读取键。 */
export async function handleCallback(): Promise<boolean> {
  try {
    const result = await auth().completeLogin()
    if (!result.ok) return false
    return Boolean(syncToken())
  } catch {
    return false
  }
}

/**
 * 同租户静默单点登录（web-shell-kit「同租户静默单点登录」）。
 * 形状由真实 IdP 决定：无会话时 prompt=none 是 303 带 error=login_required，
 * auth 域 X-Frame-Options: SAMEORIGIN ⇒ 静默是一次整页跳转，不可 iframe。
 */
export async function trySilentLogin(): Promise<'logged-in' | 'interactive-required'> {
  try {
    const outcome = await auth().trySilentLogin()
    if (outcome === 'logged-in') syncToken()
    return outcome
  } catch {
    return 'interactive-required'
  }
}

/** 发起一次静默授权跳转（prompt=none）。只在「本租户登录过」时调用。 */
export async function beginSilentLogin(): Promise<void> {
  await auth().beginSilentLogin()
}

export async function ensureFreshToken(): Promise<string | null> {
  try {
    const session = await auth().ensureFresh()
    if (!session) return null
    localStorage.setItem(TOKEN_KEY, session.idToken)
    return session.idToken
  } catch {
    return null
  }
}

export const setShellToken = (token: string): void => {
  localStorage.setItem(TOKEN_KEY, token)
}

export const ssoHintSeen = (): boolean => seen(SSO_HINT_DOMAIN)
export const ssoHintMark = (): void => mark(SSO_HINT_DOMAIN)

/** 登出：清本地会话后跳 Logto 的 session/end（该跳转是本仓既有行为）。 */
export function logout(): void {
  auth().logoutLocal()
  localStorage.removeItem(TOKEN_KEY)
  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    post_logout_redirect_uri: `${window.location.origin}/`,
  })
  window.location.assign(`${ISSUER_BASE}/oidc/session/end?${params}`)
}
