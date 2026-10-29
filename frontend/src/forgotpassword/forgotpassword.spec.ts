import { provideHttpClient } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { of } from 'rxjs';

import { AuthServ } from '../auth-serv';
import { ForgotPassword } from './forgotpassword';

describe('ForgotPassword', () => {
  let component: ForgotPassword;
  let fixture: ComponentFixture<ForgotPassword>;
  let authService: AuthServ;
  let router: Router;

  beforeEach(async () => {
    sessionStorage.clear();
    localStorage.clear();

    await TestBed.configureTestingModule({
      imports: [ForgotPassword],
      providers: [provideHttpClient(), provideRouter([])]
    })
    .compileComponents();

    fixture = TestBed.createComponent(ForgotPassword);
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
    expect(component.step).toBe('email');
  });

  it('should start a reset session and move to the OTP step', () => {
    spyOn(authService, 'forgotPassword').and.returnValue(of({
      message: 'If an account exists, a code has been sent.',
      challenge_id: 'challenge-id',
      resend_after_seconds: 0,
    }));

    component.emailForm.setValue({ email: 'User@Example.com' });
    component.submitEmail();

    expect(authService.forgotPassword).toHaveBeenCalledWith({
      email: 'user@example.com',
    });
    expect(component.step).toBe('otp');
    expect(component.hasChallenge).toBeTrue();
    expect(sessionStorage.getItem('reset_challenge_id')).toBe(
      'challenge-id'
    );
    expect(sessionStorage.getItem('reset_email')).toBe(
      'user@example.com'
    );
  });

  it('should verify the OTP and move to the new password step', () => {
    spyOn(authService, 'forgotPassword').and.returnValue(of({
      message: 'If an account exists, a code has been sent.',
      challenge_id: 'challenge-id',
      resend_after_seconds: 0,
    }));
    spyOn(authService, 'verifyResetOTP').and.returnValue(of({
      message: 'Code verified. You can now set a new password.',
      reset_token: 'reset-token',
      expires_in_seconds: 600,
    }));

    component.emailForm.setValue({ email: 'user@example.com' });
    component.submitEmail();
    component.otpForm.setValue({ otp: '123456' });
    component.verifyOTP();

    expect(authService.verifyResetOTP).toHaveBeenCalledWith({
      challenge_id: 'challenge-id',
      otp: '123456',
    });
    expect(component.step).toBe('password');
    expect(component.hasChallenge).toBeFalse();
    expect(sessionStorage.getItem('reset_challenge_id')).toBeNull();
  });

  it('should reset the password with the verified token and return to login', () => {
    spyOn(authService, 'forgotPassword').and.returnValue(of({
      message: 'If an account exists, a code has been sent.',
      challenge_id: 'challenge-id',
      resend_after_seconds: 0,
    }));
    spyOn(authService, 'verifyResetOTP').and.returnValue(of({
      message: 'Code verified.',
      reset_token: 'reset-token',
      expires_in_seconds: 600,
    }));
    spyOn(authService, 'resetPassword').and.returnValue(of({
      message: 'Password reset successfully. You can now log in.',
    }));
    spyOn(authService, 'savetoken');

    component.emailForm.setValue({ email: 'user@example.com' });
    component.submitEmail();
    component.otpForm.setValue({ otp: '123456' });
    component.verifyOTP();
    component.passwordForm.setValue({
      password: 'NewPassword456!',
      confirmpassword: 'NewPassword456!',
    });
    component.submitPassword();

    expect(authService.resetPassword).toHaveBeenCalledWith({
      reset_token: 'reset-token',
      password: 'NewPassword456!',
    });
    expect(authService.savetoken).not.toHaveBeenCalled();
    expect(sessionStorage.getItem('auth_success_message')).toContain(
      'Password reset successfully'
    );
    expect(router.navigate).toHaveBeenCalledWith(['/auth']);
  });

  it('should reject mismatched passwords without calling the API', () => {
    spyOn(authService, 'forgotPassword').and.returnValue(of({
      message: 'If an account exists, a code has been sent.',
      challenge_id: 'challenge-id',
      resend_after_seconds: 0,
    }));
    spyOn(authService, 'verifyResetOTP').and.returnValue(of({
      message: 'Code verified.',
      reset_token: 'reset-token',
      expires_in_seconds: 600,
    }));
    const reset = spyOn(authService, 'resetPassword');

    component.emailForm.setValue({ email: 'user@example.com' });
    component.submitEmail();
    component.otpForm.setValue({ otp: '123456' });
    component.verifyOTP();
    component.passwordForm.setValue({
      password: 'NewPassword456!',
      confirmpassword: 'DifferentPassword456!',
    });
    component.submitPassword();

    expect(reset).not.toHaveBeenCalled();
    expect(component.errorMessage).toContain('do not match');
  });
});
