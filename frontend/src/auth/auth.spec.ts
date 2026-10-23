import { provideHttpClient } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { of, throwError } from 'rxjs';

import { AuthServ } from '../auth-serv';
import { Auth } from './auth';

describe('Auth', () => {
  let component: Auth;
  let fixture: ComponentFixture<Auth>;
  let authService: AuthServ;
  let router: Router;

  beforeEach(async () => {
    sessionStorage.clear();
    localStorage.clear();

    await TestBed.configureTestingModule({
      imports: [Auth],
      providers: [provideHttpClient(), provideRouter([])]
    })
    .compileComponents();

    fixture = TestBed.createComponent(Auth);
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

  it('should keep login as the initial mode', () => {
    expect(component.isLogin).toBeTrue();
  });

  it('should log in directly without navigating to the OTP page', () => {
    spyOn(authService, 'login').and.returnValue(of({
      access_token: 'token',
      user: {
        id: 1,
        name: 'Test User',
        email: 'user@example.com',
        role: 'user',
        email_verified: true,
      },
    }));
    spyOn(authService, 'savetoken');

    component.isloginform.setValue({
      email: 'user@example.com',
      password: 'Password123!',
    });
    component.loginsubmit();

    expect(authService.savetoken).toHaveBeenCalledWith('token');
    expect(router.navigate).toHaveBeenCalledWith(['/dashboard']);
  });

  it('should show the backend error when login is rejected', () => {
    spyOn(authService, 'login').and.returnValue(throwError(() => ({
      error: {
        detail: 'Please verify your email before logging in.',
      },
    })));

    component.isloginform.setValue({
      email: 'user@example.com',
      password: 'Password123!',
    });
    component.loginsubmit();

    expect(component.errorMessage).toContain('verify your email');
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it('should store the OTP session and navigate after signup', () => {
    spyOn(authService, 'signup').and.returnValue(of({
      message: 'Verification code sent.',
      challenge_id: 'challenge-id',
      resend_after_seconds: 30,
    }));

    component.isSignupform.setValue({
      name: 'Test User',
      email: 'User@Example.com',
      password: 'Password123!',
      confirmpassword: 'Password123!',
    });
    component.signupsubmit();

    expect(sessionStorage.getItem('otp_challenge_id')).toBe('challenge-id');
    expect(sessionStorage.getItem('otp_email')).toBe('user@example.com');
    expect(
      Number(sessionStorage.getItem('otp_resend_available_at'))
    ).toBeGreaterThan(Date.now());
    expect(router.navigate).toHaveBeenCalledWith(['/otp']);
  });

  it('should stay on auth when signup email delivery fails', () => {
    spyOn(authService, 'signup').and.returnValue(throwError(() => ({
      error: {
        detail: 'Unable to send the verification email. Please try again.',
      },
    })));

    component.isSignupform.setValue({
      name: 'Test User',
      email: 'user@example.com',
      password: 'Password123!',
      confirmpassword: 'Password123!',
    });
    component.signupsubmit();

    expect(component.errorMessage).toContain('verification email');
    expect(sessionStorage.getItem('otp_challenge_id')).toBeNull();
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it('should keep the Google sign-in flow working', () => {
    spyOn(authService, 'googleLogin').and.returnValue(of({
      access_token: 'google-token',
      user: {
        id: 2,
        name: 'Google User',
        email: 'google@example.com',
        role: 'user',
        email_verified: true,
      },
    }));
    spyOn(authService, 'savetoken');

    component.handleGoogleSignIn('credential');

    expect(authService.googleLogin).toHaveBeenCalledWith('credential');
    expect(authService.savetoken).toHaveBeenCalledWith('google-token');
    expect(router.navigate).toHaveBeenCalledWith(['/dashboard']);
  });
});
