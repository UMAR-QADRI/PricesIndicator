import { Injectable } from '@angular/core';
import {
  HttpClient,
  HttpHeaders,
  HttpParams
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

export type AdminUserStatus = 'active' | 'blocked' | 'inactive';

export interface AdminUser {
  id: number;
  name: string;
  email: string;
  role: 'admin' | 'user';
  email_verified: boolean;
  status: AdminUserStatus;
  is_blocked: boolean;
  auth_provider: string;
  created_at: string;
  last_login_at: string | null;
  last_login_method: string | null;
}

export interface AdminUserPage {
  items: AdminUser[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface AdminLoginEvent {
  id: number;
  method: string;
  logged_in_at: string;
}

export interface AdminLoginHistory {
  items: AdminLoginEvent[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
  last_login_at: string | null;
  last_login_method: string | null;
}

export interface AdminUserUpdate {
  name?: string;
  email?: string;
  role?: 'admin' | 'user';
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

  getAdminUsers(options: {
    search?: string;
    status?: AdminUserStatus | 'all';
    page?: number;
    pageSize?: number;
  } = {}): Observable<AdminUserPage> {
    let params = new HttpParams()
      .set('page', String(options.page ?? 1))
      .set('page_size', String(options.pageSize ?? 10))
      .set('status', options.status ?? 'all');

    const search = options.search?.trim();

    if (search) {
      params = params.set('search', search);
    }

    return this.http.get<AdminUserPage>(
      `${this.apiUrl}/admin/users`,
      {
        headers: this.getAuthHeaders(),
        params
      }
    );
  }

  getAdminUser(userId: number): Observable<AdminUser> {
    return this.http.get<AdminUser>(
      `${this.apiUrl}/admin/users/${userId}`,
      { headers: this.getAuthHeaders() }
    );
  }

  getAdminUserLogins(
    userId: number,
    options: { page?: number; pageSize?: number } = {}
  ): Observable<AdminLoginHistory> {
    const params = new HttpParams()
      .set('page', String(options.page ?? 1))
      .set('page_size', String(options.pageSize ?? 20));

    return this.http.get<AdminLoginHistory>(
      `${this.apiUrl}/admin/users/${userId}/logins`,
      {
        headers: this.getAuthHeaders(),
        params
      }
    );
  }

  updateAdminUser(
    userId: number,
    data: AdminUserUpdate
  ): Observable<AdminUser> {
    return this.http.patch<AdminUser>(
      `${this.apiUrl}/admin/users/${userId}`,
      data,
      { headers: this.getAuthHeaders() }
    );
  }

  blockAdminUser(userId: number): Observable<AdminUser> {
    return this.http.post<AdminUser>(
      `${this.apiUrl}/admin/users/${userId}/block`,
      {},
      { headers: this.getAuthHeaders() }
    );
  }

  unblockAdminUser(userId: number): Observable<AdminUser> {
    return this.http.post<AdminUser>(
      `${this.apiUrl}/admin/users/${userId}/unblock`,
      {},
      { headers: this.getAuthHeaders() }
    );
  }

  deleteAdminUser(userId: number): Observable<{
    message: string;
    user_id: number;
  }> {
    return this.http.delete<{
      message: string;
      user_id: number;
    }>(
      `${this.apiUrl}/admin/users/${userId}`,
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