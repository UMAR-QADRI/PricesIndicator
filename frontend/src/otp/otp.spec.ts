import { provideHttpClient } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { of, throwError } from 'rxjs';

import { AuthServ } from '../auth-serv';
import { Otp } from './otp';

describe('Otp', () => {
  let component: Otp;
  let fixture: ComponentFixture<Otp>;
  let authService: AuthServ;
  let router: Router;

  beforeEach(async () => {
    sessionStorage.clear();
    localStorage.clear();

    await TestBed.configureTestingModule({
      imports: [Otp],
      providers: [provideHttpClient(), provideRouter([])]
    })
    .compileComponents();

    fixture = TestBed.createComponent(Otp);
    component = fixture.componentInstance;
    authService = TestBed.inject(AuthServ);
    router = TestBed.inject(Router);
    spyOn(router, 'navigate').and.resolveTo(true);
    fixture.detectChanges();
  });

  afterEach(() => {
    sessionStorage.clear();
    localStorage.clear();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should return to login after verifying without storing a token', () => {
    sessionStorage.setItem('otp_challenge_id', 'challenge-id');
    sessionStorage.setItem('otp_email', 'user@example.com');
    sessionStorage.setItem(
      'otp_resend_available_at',
      String(Date.now() + 30000)
    );
    spyOn(authService, 'verifyOTP').and.returnValue(of({
      message: 'Email verified successfully. You can now log in.',
      user: {
        id: 1,
        name: 'Test User',
        email: 'user@example.com',
        role: 'user',
        email_verified: true,
      },
    }));
    spyOn(authService, 'savetoken');

    component.otpForm.setValue({ otp: '123456' });
    component.verifyOTP();

    expect(authService.savetoken).not.toHaveBeenCalled();
    expect(sessionStorage.getItem('otp_challenge_id')).toBeNull();
    expect(sessionStorage.getItem('otp_email')).toBeNull();
    expect(sessionStorage.getItem('otp_resend_available_at')).toBeNull();
    expect(sessionStorage.getItem('auth_success_message')).toContain(
      'Email verified'
    );
    expect(router.navigate).toHaveBeenCalledWith(['/auth']);
  });

  it('should not call the API without a stored challenge', () => {
    const verify = spyOn(authService, 'verifyOTP');

    component.otpForm.setValue({ otp: '123456' });
    component.verifyOTP();

    expect(verify).not.toHaveBeenCalled();
    expect(component.hasChallenge).toBeFalse();
    expect(component.errorMessage).toContain('expired');
  });

  it('should display the signup email and restored resend cooldown', () => {
    sessionStorage.setItem('otp_challenge_id', 'challenge-id');
    sessionStorage.setItem('otp_email', 'user@example.com');
    sessionStorage.setItem(
      'otp_resend_available_at',
      String(Date.now() + 30000)
    );

    const restoredFixture = TestBed.createComponent(Otp);
    const restored = restoredFixture.componentInstance;
    restoredFixture.detectChanges();

    expect(restored.email).toBe('user@example.com');
    expect(restored.hasChallenge).toBeTrue();
    expect(restored.resendSeconds).toBeGreaterThan(0);

    restoredFixture.destroy();
  });

  it('should not resend while the cooldown is active', () => {
    sessionStorage.setItem('otp_challenge_id', 'challenge-id');
    component.hasChallenge = true;
    component.resendSeconds = 12;
    const resend = spyOn(authService, 'resendOTP');

    component.resendOTP();

    expect(resend).not.toHaveBeenCalled();
  });

  it('should rotate the challenge and restart cooldown after resending', () => {
    sessionStorage.setItem('otp_challenge_id', 'challenge-id');
    component.hasChallenge = true;
    spyOn(authService, 'resendOTP').and.returnValue(of({
      message: 'A new code has been sent.',
      challenge_id: 'new-challenge-id',
      resend_after_seconds: 30,
    }));

    component.resendOTP();

    expect(sessionStorage.getItem('otp_challenge_id')).toBe(
      'new-challenge-id'
    );
    expect(component.resendSeconds).toBeGreaterThan(0);
    expect(component.successMessage).toContain('new code');
  });

  it('should honor Retry-After when resend is throttled', () => {
    sessionStorage.setItem('otp_challenge_id', 'challenge-id');
    component.hasChallenge = true;
    spyOn(authService, 'resendOTP').and.returnValue(throwError(() => ({
      error: { detail: 'Please wait before requesting another code.' },
      headers: {
        get: (name: string) => name === 'Retry-After' ? '17' : null
      }
    })));

    component.resendOTP();

    expect(component.errorMessage).toContain('Please wait');
    expect(component.resendSeconds).toBeGreaterThan(0);
  });
});
