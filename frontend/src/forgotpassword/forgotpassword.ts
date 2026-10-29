import { CommonModule } from '@angular/common';
import { Component, OnDestroy } from '@angular/core';
import {
  FormBuilder,
  FormGroup,
  ReactiveFormsModule,
  Validators
} from '@angular/forms';
import { Router } from '@angular/router';
import { AuthServ } from '../auth-serv';

type ResetStep = 'email' | 'otp' | 'password';

@Component({
  selector: 'app-forgot-password',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule
  ],
  templateUrl: './forgotpassword.html'
})
export class ForgotPassword implements OnDestroy {
  step: ResetStep = 'email';
  isLoading = false;
  isResending = false;
  errorMessage = '';
  successMessage = '';
  email = '';
  hasChallenge = false;
  resendSeconds = 0;

  emailForm: FormGroup;
  otpForm: FormGroup;
  passwordForm: FormGroup;

  private challengeId = '';
  private resetToken = '';
  private cooldownTimer?: number;

  constructor(
    private fb: FormBuilder,
    private auth: AuthServ,
    private router: Router
  ) {
    this.emailForm = this.fb.group({
      email: ['', [Validators.required, Validators.email]]
    });

    this.otpForm = this.fb.group({
      otp: [
        '',
        [
          Validators.required,
          Validators.pattern(/^\d{6}$/)
        ]
      ]
    });

    this.passwordForm = this.fb.group({
      password: ['', [Validators.required, Validators.minLength(8)]],
      confirmpassword: [
        '',
        [Validators.required, Validators.minLength(8)]
      ]
    });

    const storedChallenge = sessionStorage.getItem(
      'reset_challenge_id'
    );

    if (storedChallenge) {
      this.challengeId = storedChallenge;
      this.email = sessionStorage.getItem('reset_email') ?? '';
      this.hasChallenge = true;
      this.step = 'otp';
    }

    this.startCooldownFromStorage();
  }

  ngOnDestroy(): void {
    this.stopCooldownTimer();
  }

  submitEmail(): void {
    if (this.emailForm.invalid || this.isLoading) {
      this.emailForm.markAllAsTouched();
      return;
    }

    this.isLoading = true;
    this.errorMessage = '';
    this.successMessage = '';

    const email = String(
      this.emailForm.getRawValue().email ?? ''
    ).trim().toLowerCase();

    this.auth.forgotPassword({ email }).subscribe({
      next: (res) => {
        this.isLoading = false;

        if (!res.challenge_id) {
          this.errorMessage =
            'The server did not start a password reset session.';
          return;
        }

        this.email = email;
        this.challengeId = res.challenge_id;
        this.hasChallenge = true;
        this.successMessage = res.message ?? '';
        this.setCooldownSeconds(res.resend_after_seconds ?? 0);

        sessionStorage.setItem(
          'reset_challenge_id',
          res.challenge_id
        );
        sessionStorage.setItem('reset_email', email);

        this.otpForm.reset();
        this.step = 'otp';
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage =
          err.error?.detail ||
          'Unable to start password recovery. Please try again.';
      }
    });
  }

  verifyOTP(): void {
    if (!this.challengeId) {
      this.hasChallenge = false;
      this.errorMessage =
        'Your password reset session has expired. Please start again.';
      return;
    }

    if (this.otpForm.invalid || this.isLoading) {
      this.otpForm.markAllAsTouched();
      return;
    }

    this.isLoading = true;
    this.errorMessage = '';
    this.successMessage = '';

    const otp = this.otpForm.getRawValue().otp;

    if (typeof otp !== 'string') {
      this.isLoading = false;
      this.errorMessage = 'Please enter your reset code.';
      return;
    }

    this.auth.verifyResetOTP({
      challenge_id: this.challengeId,
      otp
    }).subscribe({
      next: (res) => {
        this.isLoading = false;

        if (!res.reset_token) {
          this.errorMessage =
            'The server did not authorize a password reset.';
          return;
        }

        this.resetToken = res.reset_token;
        this.challengeId = '';
        this.hasChallenge = false;
        this.successMessage =
          res.message || 'Code verified. Enter your new password.';
        this.stopCooldownTimer();
        this.resendSeconds = 0;
        this.auth.clearPasswordResetSession();
        this.passwordForm.reset();
        this.step = 'password';
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage =
          err.error?.detail ||
          'Unable to verify your reset code. Please try again.';
      }
    });
  }

  submitPassword(): void {
    if (this.passwordForm.invalid || this.isLoading) {
      this.passwordForm.markAllAsTouched();
      return;
    }

    if (!this.resetToken) {
      this.errorMessage =
        'Your password reset authorization has expired. Please start again.';
      return;
    }

    const { password, confirmpassword } =
      this.passwordForm.getRawValue();

    if (password !== confirmpassword) {
      this.errorMessage = 'Passwords do not match.';
      return;
    }

    this.isLoading = true;
    this.errorMessage = '';
    this.successMessage = '';

    this.auth.resetPassword({
      reset_token: this.resetToken,
      password
    }).subscribe({
      next: (res) => {
        this.isLoading = false;
        this.resetToken = '';
        this.auth.clearPasswordResetSession();
        sessionStorage.setItem(
          'auth_success_message',
          res.message ||
            'Password reset successfully. You can now log in.'
        );

        void this.router.navigate(['/auth']);
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage =
          err.error?.detail ||
          'Unable to reset your password. Please try again.';
      }
    });
  }

  resendOTP(): void {
    if (this.isResending || this.isLoading || this.resendSeconds > 0) {
      return;
    }

    if (!this.challengeId) {
      this.hasChallenge = false;
      this.errorMessage =
        'Your password reset session has expired. Please start again.';
      return;
    }

    this.isResending = true;
    this.errorMessage = '';
    this.successMessage = '';

    this.auth.resendResetOTP({
      challenge_id: this.challengeId
    }).subscribe({
      next: (res) => {
        this.isResending = false;

        if (!res.challenge_id) {
          this.errorMessage =
            'The server did not return a new reset session.';
          return;
        }

        this.challengeId = res.challenge_id;
        this.hasChallenge = true;
        sessionStorage.setItem(
          'reset_challenge_id',
          res.challenge_id
        );
        this.setCooldownSeconds(res.resend_after_seconds ?? 0);
        this.otpForm.reset();
        this.successMessage =
          res.message ||
          'A new password reset code has been sent to your email.';
      },
      error: (err) => {
        this.isResending = false;
        this.errorMessage =
          err.error?.detail ||
          'Unable to resend the reset code. Please try again.';

        const retryAfter = Number(
          err.headers?.get('Retry-After')
        );

        if (Number.isFinite(retryAfter) && retryAfter > 0) {
          this.setCooldownSeconds(retryAfter);
        }
      }
    });
  }

  backToLogin(): void {
    this.auth.clearPasswordResetSession();
    this.stopCooldownTimer();
    this.hasChallenge = false;
    void this.router.navigate(['/auth']);
  }

  private startCooldownFromStorage(): void {
    const availableAt = Number(
      sessionStorage.getItem('reset_resend_available_at')
    );

    if (Number.isFinite(availableAt) && availableAt > 0) {
      this.setCooldownUntil(availableAt);
    }
  }

  private setCooldownSeconds(seconds: number): void {
    this.setCooldownUntil(Date.now() + seconds * 1000);
  }

  private setCooldownUntil(availableAt: number): void {
    this.stopCooldownTimer();

    const update = (): void => {
      this.resendSeconds = Math.max(
        0,
        Math.ceil((availableAt - Date.now()) / 1000)
      );

      if (this.resendSeconds === 0) {
        this.stopCooldownTimer();
        sessionStorage.removeItem('reset_resend_available_at');
      }
    };

    update();

    if (this.resendSeconds > 0) {
      sessionStorage.setItem(
        'reset_resend_available_at',
        String(availableAt)
      );
      this.cooldownTimer = window.setInterval(update, 1000);
    }
  }

  private stopCooldownTimer(): void {
    if (this.cooldownTimer !== undefined) {
      window.clearInterval(this.cooldownTimer);
      this.cooldownTimer = undefined;
    }
  }
}
