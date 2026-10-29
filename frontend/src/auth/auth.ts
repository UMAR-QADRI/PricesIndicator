
import {
  AfterViewInit,
  Component,
} from '@angular/core';

import { CommonModule } from '@angular/common';
import {
  FormBuilder,
  FormGroup,
  FormsModule,
  ReactiveFormsModule,
  Validators,
} from '@angular/forms';

import { Router } from '@angular/router';
import { AuthServ } from '../auth-serv';

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: {
            client_id: string;
            callback: (response: { credential: string }) => void;
          }) => void;

          renderButton: (
            parent: HTMLElement,
            options: {
              theme?: string;
              size?: string;
              text?: string;
              shape?: string;
              width?: number;
            }
          ) => void;
        };
      };
    };
  }
}

@Component({
  selector: 'app-auth',
  standalone: true,
  imports: [CommonModule, ReactiveFormsModule, FormsModule],
  templateUrl: './auth.html',
  styleUrl: './auth.css',
})
export class Auth implements AfterViewInit {
  isLogin = true;

  isloginform: FormGroup;
  isSignupform: FormGroup;

  isLoading = false;
  isGoogleLoading = false;
  errorMessage = '';
  successMessage = '';

  private googleInitialized = false;

  constructor(
    private fb: FormBuilder,
    private router: Router,
    private authe: AuthServ
  ) {
    const successMessage = sessionStorage.getItem(
      'auth_success_message'
    );

    if (successMessage) {
      this.successMessage = successMessage;
      sessionStorage.removeItem('auth_success_message');
    }

    this.isloginform = this.fb.group({
      email: ['', [Validators.required, Validators.email]],
      password: ['', [Validators.required, Validators.minLength(8)]],
    });

    this.isSignupform = this.fb.group({
      name: ['', [Validators.required, Validators.maxLength(100)]],
      email: ['', [Validators.required, Validators.email]],
      password: ['', [Validators.required, Validators.minLength(8)]],
      confirmpassword: ['', [Validators.required, Validators.minLength(8)]],
    });
  }

  ngAfterViewInit(): void {
    this.initializeGoogleSignIn();
  }

  private initializeGoogleSignIn(): void {
    const google = window.google;

    if (!google?.accounts?.id || this.googleInitialized) {
      return;
    }

    google.accounts.id.initialize({
      client_id:
        '970728226507-umke92splgbk6ih3b11iub8dj6nk91r8.apps.googleusercontent.com',

      callback: (response: { credential: string }) => {
        this.handleGoogleSignIn(response.credential);
      },
    });

    this.googleInitialized = true;
    this.renderGoogleButton();
  }

  private renderGoogleButton(): void {
    const google = window.google?.accounts.id;

    if (!google) {
      return;
    }

    const container = document.getElementById('googleSignInButton');

    if (container) {
      container.replaceChildren();

      google.renderButton(container, {
        theme: 'outline',
        size: 'large',
        text: 'continue_with',
        shape: 'rectangular',
        width: 280,
      });
    }
  }

  handleGoogleSignIn(idToken: string): void {
    if (this.isGoogleLoading) {
      return;
    }

    this.isGoogleLoading = true;
    this.errorMessage = '';

    this.authe.googleLogin(idToken).subscribe({
      next: (res) => {
        this.isGoogleLoading = false;

        if (!res.access_token) {
          this.errorMessage = 'The server returned no access token.';
          return;
        }

        this.authe.savetoken(res.access_token);
        void this.router.navigate([
          res.user?.role === 'admin' ? '/admin' : '/dashboard'
        ]);
      },
      error: (err) => {
        this.isGoogleLoading = false;
        this.errorMessage =
          err.error?.detail || 'Unable to sign in with Google.';
      },
    });
  }

  loginsubmit(): void {
    if (this.isloginform.invalid || this.isLoading) {
      this.isloginform.markAllAsTouched();
      return;
    }

    this.isLoading = true;
    this.errorMessage = '';
    this.successMessage = '';

    this.authe.login(this.isloginform.getRawValue()).subscribe({
      next: (res) => {
        this.isLoading = false;

        if (!res.access_token || !res.user) {
          this.errorMessage = 'Invalid response from the server.';
          return;
        }

        this.authe.savetoken(res.access_token);
        this.authe.clearOtpSession();

        void this.router.navigate([
          res.user.role === 'admin' ? '/admin' : '/dashboard'
        ]);
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage =
          err.error?.detail || 'Login failed. Please try again.';
      },
    });
  }

  signupsubmit(): void {
    if (this.isSignupform.invalid || this.isLoading) {
      this.isSignupform.markAllAsTouched();
      return;
    }

    const { name, email, password, confirmpassword } =
      this.isSignupform.getRawValue();

    if (password !== confirmpassword) {
      this.errorMessage = 'Passwords do not match.';
      return;
    }

    this.isLoading = true;
    this.errorMessage = '';
    this.successMessage = '';

    this.authe.signup({
      name: name.trim(),
      email: email.trim(),
      password,
    }).subscribe({
      next: (res) => {
        this.isLoading = false;

        if (!res.challenge_id) {
          this.errorMessage = 'Invalid response from the server.';
          return;
        }

        sessionStorage.setItem(
          'otp_challenge_id',
          res.challenge_id
        );
        sessionStorage.setItem(
          'otp_email',
          email.trim().toLowerCase()
        );

        const cooldownSeconds = Math.max(
          0,
          res.resend_after_seconds ?? 0
        );
        sessionStorage.setItem(
          'otp_resend_available_at',
          String(Date.now() + cooldownSeconds * 1000)
        );

        void this.router.navigate(['/otp']);
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage =
          err.error?.detail || 'Signup failed. Please try again.';
      },
    });
  }

  openForgotPassword(): void {
    this.errorMessage = '';
    this.successMessage = '';
    void this.router.navigate(['/forgot-password']);
  }

  toggleform(): void {
    this.isLogin = !this.isLogin;
    this.errorMessage = '';
    this.successMessage = '';

    setTimeout(() => this.renderGoogleButton(), 0);
  }
}
