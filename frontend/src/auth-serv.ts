import { Injectable } from '@angular/core';
import {
  HttpClient,
  HttpHeaders
} from '@angular/common/http';
import { Observable } from 'rxjs';

export interface AuthResponse {
  message?: string;
  challenge_id?: string;
  resend_after_seconds?: number;
  access_token?: string;
  token_type?: string;
  user?: AuthUser;
}

export interface AuthUser {
  id: number;
  name: string;
  email: string;
  role: 'admin' | 'user';
  email_verified: boolean;
}

@Injectable({
  providedIn: 'root'
})
export class AuthServ {
  private readonly apiUrl = 'http://localhost:8000';

  constructor(private http: HttpClient) {}

  signup(data: {
    name: string;
    email: string;
    password: string;
  }): Observable<AuthResponse> {
    return this.http.post<AuthResponse>(
      `${this.apiUrl}/auth/signup`,
      data
    );
  }

  login(data: {
    email: string;
    password: string;
  }): Observable<AuthResponse> {
    return this.http.post<AuthResponse>(
      `${this.apiUrl}/auth/login`,
      data
    );
  }

  verifyOTP(data: {
    challenge_id: string;
    otp: string;
  }): Observable<AuthResponse> {
    return this.http.post<AuthResponse>(
      `${this.apiUrl}/auth/verify-otp`,
      data
    );
  }

  resendOTP(data: {
    challenge_id: string;
  }): Observable<AuthResponse> {
    return this.http.post<AuthResponse>(
      `${this.apiUrl}/auth/resend-otp`,
      data
    );
  }

  googleLogin(idToken: string): Observable<AuthResponse> {
    return this.http.post<AuthResponse>(
      `${this.apiUrl}/auth/google`,
      { id_token: idToken }
    );
  }

  getMe(): Observable<AuthUser> {
    return this.http.get<AuthUser>(
      `${this.apiUrl}/auth/me`,
      { headers: this.getAuthHeaders() }
    );
  }

  checkAdminAccess(): Observable<{
    message: string;
    user: AuthUser;
  }> {
    return this.http.get<{
      message: string;
      user: AuthUser;
    }>(
      `${this.apiUrl}/admin/check`,
      { headers: this.getAuthHeaders() }
    );
  }

  savetoken(token: string): void {
    localStorage.setItem('access_token', token);
  }

  getToken(): string | null {
    return localStorage.getItem('access_token');
  }

  getAuthHeaders(): HttpHeaders {
    const token = this.getToken();

    return new HttpHeaders({
      Authorization: `Bearer ${token ?? ''}`
    });
  }

  clearOtpSession(): void {
    sessionStorage.removeItem('otp_challenge_id');
    sessionStorage.removeItem('otp_email');
    sessionStorage.removeItem('otp_resend_available_at');
  }

  clearSession(): void {
    localStorage.removeItem('access_token');
    this.clearOtpSession();
  }

  logout(): void {
    this.clearSession();
  }
}