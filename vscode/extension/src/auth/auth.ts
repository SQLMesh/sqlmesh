import {
  env,
  Uri,
  AuthenticationProvider,
  AuthenticationProviderAuthenticationSessionsChangeEvent,
  AuthenticationSession,
  Event,
  EventEmitter,
  window,
} from 'vscode'
import { getTcloudBin } from '../utilities/sqlmesh/sqlmesh'
import { err, isErr, ok, Result } from '@bus/result'
import { execAsync } from '../utilities/exec'
import { getProjectRoot } from '../utilities/common/utilities'
import z from 'zod'
import { traceError } from '../utilities/common/log'
import { ErrorType } from '../utilities/errors'

export const AUTH_TYPE = 'tobikodata'
export const AUTH_NAME = 'Tobiko'

const tokenSchema = z.object({
  iss: z.string(),
  aud: z.string(),
  sub: z.string(),
  scope: z.string(),
  iat: z.number(),
  exp: z.number(),
  email: z.string(),
})

const statusResponseSchema = z.discriminatedUnion('is_logged_in', [
  z.object({
    is_logged_in: z.literal(true),
    id_token: tokenSchema,
  }),
  z.object({
    is_logged_in: z.literal(false),
    id_token: z.object({}),
  }),
])

type StatusResponse = z.infer<typeof statusResponseSchema>

const loginUrlResponseSchema = z.object({
  url: z.string(),
  verifier_code: z.string(),
})

const deviceCodeResponseSchema = z.object({
  device_code: z.string(),
  user_code: z.string(),
  verification_uri: z.string(),
  verification_uri_complete: z.string(),
  expires_in: z.number(),
})

export class AuthenticationProviderTobikoCloud
  implements AuthenticationProvider
{
  static id = AUTH_TYPE
  static name = AUTH_NAME

  private _sessionChangeEmitter =
    new EventEmitter<AuthenticationProviderAuthenticationSessionsChangeEvent>()

  onDidChangeSessions: Event<AuthenticationProviderAuthenticationSessionsChangeEvent> =
    this._sessionChangeEmitter.event

  /**
   * Get the status of the authentication provider from the cli
   * @returns true if the user is logged in with the id token, false otherwise
   */
  private async get_status(): Promise<Result<StatusResponse, ErrorType>> {
    const workspacePath = await getProjectRoot()
    const tcloudBin = await getTcloudBin()
    if (isErr(tcloudBin)) {
      return tcloudBin
    }
    const tcloudBinPath = tcloudBin.value
    const result = await execAsync(
      tcloudBinPath.bin,
      ['auth', 'vscode', 'status'],
      {
        cwd: workspacePath.uri.fsPath,
        env: tcloudBinPath.env,
      },
    )
    if (result.exitCode !== 0) {
      return err({
        type: 'generic',
        message: 'Failed to get tcloud auth status',
      })
    }
    const status = result.stdout
    const statusToJson: any = JSON.parse(status)
    const statusResponse = statusResponseSchema.parse(statusToJson)
    return ok(statusResponse)
  }

  async getSessions(): Promise<AuthenticationSession[]> {
    const status = await this.get_status()
    if (isErr(status)) {
      return []
    }
    const statusResponse = status.value
    if (!statusResponse.is_logged_in) {
      return []
    }
    const token = statusResponse.id_token
    if (!token) {
      throw new Error('Invalid state from tcloud, failed to get token.')
    }
    const session = {
      id: token.email,
      account: {
        id: token.sub,
        label: token.email,
      },
      scopes: token.scope.split(' '),
      accessToken: '',
    }
    return [session]
  }

  async createSession(): Promise<AuthenticationSession> {
    await this.sign_in_oauth_flow()
    const status = await this.get_status()
    if (isErr(status)) {
      throw new Error('Failed to get tcloud auth status')
    }
    const statusResponse = status.value
    if (!statusResponse.is_logged_in) {
      throw new Error('Failed to login to tcloud')
    }
    const token = statusResponse.id_token
    if (!token) {
      throw new Error('Failed to get tcloud token')
    }
    const session: AuthenticationSession = {
      id: token.email,
      account: {
        id: token.email,
        label: 'Tobiko',
      },
      scopes: token.scope.split(' '),
      accessToken: '',
    }
    this._sessionChangeEmitter.fire({
      added: [session],
      removed: [],
      changed: [],
    })
    return session
  }

  async removeSession(): Promise<void> {
    // Get current sessions before logging out
    const currentSessions = await this.getSessions()
    const tcloudBin = await getTcloudBin()
    const workspacePath = await getProjectRoot()
    if (isErr(tcloudBin)) {
      throw new Error('Failed to get tcloud bin')
    }
    const tcloudBinPath = tcloudBin.value
    const result = await execAsync(tcloudBinPath.bin, ['auth', 'logout'], {
      cwd: workspacePath.uri.fsPath,
      env: tcloudBinPath.env,
    })
    if (result.exitCode !== 0) {
      throw new Error('Failed to logout from tcloud')
    }

    // Emit event with the actual sessions that were removed
    if (currentSessions.length > 0) {
      this._sessionChangeEmitter.fire({
        added: [],
        removed: currentSessions,
        changed: [],
      })
    }
  }

  async sign_in_oauth_flow(): Promise<void> {
    const workspacePath = await getProjectRoot()
    const tcloudBin = await getTcloudBin()
    if (isErr(tcloudBin)) {
      throw new Error('Failed to get tcloud bin')
    }
    const tcloudBinPath = tcloudBin.value
    const result = await execAsync(
      tcloudBinPath.bin,
      ['auth', 'vscode', 'login-url'],
      {
        cwd: workspacePath.uri.fsPath,
        env: tcloudBinPath.env,
      },
    )
    if (result.exitCode !== 0) {
      throw new Error('Failed to get tcloud login url')
    }

    try {
      const resultToJson: any = JSON.parse(result.stdout)
      const urlCode = loginUrlResponseSchema.parse(resultToJson)
      const url = urlCode.url

      if (!url) {
        throw new Error('Invalid login URL received')
      }

      const ac = new AbortController()
      const timeout = setTimeout(
        () => {
          ac.abort()
        },
        1000 * 60 * 5,
      )
      const backgroundServerForLogin = execAsync(
        tcloudBinPath.bin,
        ['auth', 'vscode', 'start-server', urlCode.verifier_code],
        {
          cwd: workspacePath.uri.fsPath,
          signal: ac.signal,
          env: tcloudBinPath.env,
        },
      )

      const messageResult = await window.showInformationMessage(
        'Please login to Tobiko Cloud',
        {
          modal: true,
        },
        'Sign in with browser',
        'Cancel',
      )

      if (messageResult === 'Sign in with browser') {
        await env.openExternal(Uri.parse(url))
      } else {
        // Always abort the server if not proceeding with sign in
        ac.abort()
        clearTimeout(timeout)
        if (messageResult === 'Cancel') {
          throw new Error('Login cancelled')
        }
        return
      }

      try {
        const output = await backgroundServerForLogin
        if (output.exitCode !== 0) {
          throw new Error(`Failed to complete authentication: ${output.stderr}`)
        }
        // Get updated session and notify about the change
        const sessions = await this.getSessions()
        if (sessions.length > 0) {
          this._sessionChangeEmitter.fire({
            added: sessions,
            removed: [],
            changed: [],
          })
        }
      } catch (error) {
        if (error instanceof Error && error.name === 'AbortError') {
          throw new Error('Authentication timeout or aborted')
        }
        traceError(`Server error: ${error}`)
        throw error
      } finally {
        clearTimeout(timeout)
      }
    } catch (error) {
      if (error instanceof Error && error.message === 'Login cancelled') {
        throw error
      }
      traceError(`Authentication flow error: ${error}`)
      throw new Error('Failed to complete authentication flow')
    }
  }

  async sign_in_device_flow(): Promise<void> {
    const workspacePath = await getProjectRoot()
    const tcloudBin = await getTcloudBin()
    if (isErr(tcloudBin)) {
      throw new Error('Failed to get tcloud bin')
    }
    const tcloudBinPath = tcloudBin.value
    const result = await execAsync(
      tcloudBinPath.bin,
      ['auth', 'vscode', 'device'],
      {
        cwd: workspacePath.uri.fsPath,
        env: tcloudBinPath.env,
      },
    )
    if (result.exitCode !== 0) {
      throw new Error('Failed to get device code')
    }

    try {
      const resultToJson: any = JSON.parse(result.stdout)
      const deviceCodeResponse = deviceCodeResponseSchema.parse(resultToJson)

      const ac = new AbortController()
      const timeout = setTimeout(
        () => {
          ac.abort()
        },
        1000 * 60 * 5,
      )
      const waiting = execAsync(
        tcloudBinPath.bin,
        ['auth', 'vscode', 'poll_device', deviceCodeResponse.device_code],
        {
          cwd: workspacePath.uri.fsPath,
          signal: ac.signal,
          env: tcloudBinPath.env,
        },
      )

      const messageResult = await window.showInformationMessage(
        `Confirm the code ${deviceCodeResponse.user_code} at ${deviceCodeResponse.verification_uri}`,
        {
          modal: true,
        },
        'Open browser',
        'Cancel',
      )

      if (messageResult === 'Open browser') {
        await env.openExternal(
          Uri.parse(deviceCodeResponse.verification_uri_complete),
        )
      }
      if (messageResult === 'Cancel') {
        ac.abort()
        throw new Error('Login cancelled')
      }

      try {
        const output = await waiting
        if (output.exitCode !== 0) {
          throw new Error(`Failed to authenticate: ${output.stderr}`)
        }

        // Get updated session and notify about the change
        const sessions = await this.getSessions()
        if (sessions.length > 0) {
          this._sessionChangeEmitter.fire({
            added: sessions,
            removed: [],
            changed: [],
          })
        }
      } catch (error) {
        traceError(`Authentication error: ${error}`)
        throw error
      } finally {
        clearTimeout(timeout)
      }
    } catch (error) {
      traceError(`JSON parsing error: ${error}`)
      throw new Error('Failed to parse device code response')
    }
  }
}

/**
 * Checks if the user is currently signed into Tobiko Cloud.
 * @returns A promise that resolves to true if the user is signed in, false otherwise.
 */
export async function isSignedIntoTobikoCloud(): Promise<boolean> {
  try {
    const authProvider = new AuthenticationProviderTobikoCloud()
    const sessions = await authProvider.getSessions()
    return sessions.length > 0
  } catch (error) {
    traceError(`Error checking authentication status: ${error}`)
    return false
  }
}
