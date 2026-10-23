import { provideHttpClient } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';

import { AuthServ } from './auth-serv';

describe('AuthServ', () => {
  let service: AuthServ;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient()]
    });
    service = TestBed.inject(AuthServ);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should clear the token and OTP session on logout', () => {
    localStorage.setItem('access_token', 'token');
    sessionStorage.setItem('otp_challenge_id', 'challenge');
    sessionStorage.setItem('otp_email', 'user@example.com');
    sessionStorage.setItem('otp_resend_available_at', '123');

    service.logout();

    expect(localStorage.getItem('access_token')).toBeNull();
    expect(sessionStorage.getItem('otp_challenge_id')).toBeNull();
    expect(sessionStorage.getItem('otp_email')).toBeNull();
    expect(sessionStorage.getItem('otp_resend_available_at')).toBeNull();
  });
});
