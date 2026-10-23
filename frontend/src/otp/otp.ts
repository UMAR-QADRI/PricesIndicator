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

@Component({
  selector: 'app-otp',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule
  ],
  templateUrl: './otp.html'
})
export class Otp implements OnDestroy {
  isLoading = false;
  isResending = false;
  errorMessage = '';
  successMessage = '';
  email = '';
  hasChallenge = false;
  resendSeconds = 0;

  otpForm: FormGroup;

  private cooldownTimer?: number;

  constructor(
    private fb: FormBuilder,
    private auth: AuthServ,
    private router: Router
  ) {
    this.otpForm = this.fb.group({
      otp: [
        '',
        [
          Validators.required,
          Validators.pattern(/^\d{6}$/)
        ]
      ]
    });

    this.email = sessionStorage.getItem('otp_email') ?? '';
    this.hasChallenge = Boolean(this.getChallengeId());

    if (!this.hasChallenge) {
      this.errorMessage =
        'Your verification session has expired. Please create your account again.';
    }

    this.startCooldownFromStorage();
  }

  ngOnDestroy(): void {
    this.stopCooldownTimer();
  }

  verifyOTP(): void {
    const challengeId = this.getChallengeId();

    if (!challengeId) {
      this.hasChallenge = false;
      this.errorMessage =
        'Your verification session has expired. Please create your account again.';
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
      this.errorMessage = 'Please enter your verification code.';
      return;
    }

    this.auth.verifyOTP({
      challenge_id: challengeId,
      otp
    }).subscribe({
      next: (res) => {
        this.isLoading = false;

        if (!res.user?.email_verified) {
          this.errorMessage =
            'Your email could not be verified. Please try again.';
          return;
        }

        this.auth.clearOtpSession();
        this.hasChallenge = false;
        sessionStorage.setItem(
          'auth_success_message',
          res.message ||
            'Email verified successfully. You can now log in.'
        );

        void this.router.navigate(['/auth']);
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage =
          err.error?.detail ||
          'Unable to verify your code. Please try again.';
      }
    });
  }

  resendOTP(): void {
    if (this.isResending || this.isLoading || this.resendSeconds > 0) {
      return;
    }

    const challengeId = this.getChallengeId();

    if (!challengeId) {
      this.hasChallenge = false;
      this.errorMessage =
        'Your verification session has expired. Please create your account again.';
      return;
    }

    this.isResending = true;
    this.errorMessage = '';
    this.successMessage = '';

    this.auth.resendOTP({ challenge_id: challengeId }).subscribe({
      next: (res) => {
        this.isResending = false;

        if (!res.challenge_id) {
          this.errorMessage =
            'The server did not return a new verification session.';
          return;
        }

        sessionStorage.setItem(
          'otp_challenge_id',
          res.challenge_id
        );
        this.hasChallenge = true;
        this.setCooldownSeconds(res.resend_after_seconds ?? 0);
        this.otpForm.reset();
        this.successMessage =
          res.message ||
          'A new verification code has been sent to your email.';
      },
      error: (err) => {
        this.isResending = false;
        this.errorMessage =
          err.error?.detail ||
          'Unable to resend the verification code. Please try again.';

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
    this.auth.clearOtpSession();
    this.hasChallenge = false;
    void this.router.navigate(['/auth']);
  }

  private getChallengeId(): string | null {
    return sessionStorage.getItem('otp_challenge_id');
  }

  private startCooldownFromStorage(): void {
    const availableAt = Number(
      sessionStorage.getItem('otp_resend_available_at')
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
        sessionStorage.removeItem('otp_resend_available_at');
      }
    };

    update();

    if (this.resendSeconds > 0) {
      sessionStorage.setItem(
        'otp_resend_available_at',
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
