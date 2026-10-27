
import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import {
  FormBuilder,
  FormGroup,
  FormsModule,
  ReactiveFormsModule,
  Validators
} from '@angular/forms';
import { Router } from '@angular/router';
import {
  AdminLoginHistory,
  AdminUser,
  AdminUserStatus,
  AdminUserUpdate,
  AuthServ,
  AuthUser
} from '../auth-serv';

@Component({
  selector: 'app-admin-dashboard',
  standalone: true,
  imports: [CommonModule, FormsModule, ReactiveFormsModule],
  templateUrl: './admindashboard.html'
})
export class AdminDashboard implements OnInit {
  user: AuthUser | null = null;
  loading = true;
  errorMessage = '';

  users: AdminUser[] = [];
  usersLoading = false;
  usersError = '';
  successMessage = '';
  actionError = '';

  searchTerm = '';
  statusFilter: AdminUserStatus | 'all' = 'all';
  page = 1;
  pageSize = 10;
  total = 0;
  pages = 1;
  actionUserId: number | null = null;

  editForm: FormGroup;
  editUser: AdminUser | null = null;
  editError = '';
  saving = false;

  deleteUser: AdminUser | null = null;
  deleting = false;
  deleteError = '';

  detailsUser: AdminUser | null = null;
  loginHistory: AdminLoginHistory | null = null;
  historyLoading = false;
  historyError = '';

  constructor(
    private fb: FormBuilder,
    private auth: AuthServ,
    private router: Router
  ) {
    this.editForm = this.fb.group({
      name: ['', [Validators.required, Validators.maxLength(100)]],
      email: ['', [Validators.required, Validators.email]]
    });
  }

  ngOnInit(): void {
    this.loadAdminAccess();
  }

  loadAdminAccess(): void {
    this.loading = true;
    this.errorMessage = '';

    // This endpoint checks the user's role on the backend.
    this.auth.checkAdminAccess().subscribe({
      next: (response) => {
        this.user = response.user;
        this.loading = false;
        this.loadUsers();
      },
      error: (error) => {
        this.loading = false;

        if (error.status === 401) {
          this.auth.clearSession();
          void this.router.navigate(['/auth']);
          return;
        }

        if (error.status === 403) {
          void this.router.navigate(['/dashboard']);
          return;
        }

        this.errorMessage =
          'Unable to verify admin access. Please try again.';
      }
    });
  }

  loadUsers(): void {
    this.usersLoading = true;
    this.usersError = '';

    this.auth.getAdminUsers({
      search: this.searchTerm,
      status: this.statusFilter,
      page: this.page,
      pageSize: this.pageSize
    }).subscribe({
      next: (response) => {
        this.users = response.items;
        this.total = response.total;
        this.page = response.page;
        this.pages = response.pages;
        this.usersLoading = false;
      },
      error: (error) => {
        this.usersLoading = false;
        this.usersError = this.apiErrorMessage(
          error,
          'Unable to load users. Please try again.'
        );
      }
    });
  }

  onSearchSubmit(): void {
    this.page = 1;
    this.loadUsers();
  }

  onStatusFilterChange(): void {
    this.page = 1;
    this.loadUsers();
  }

  clearSearch(): void {
    this.searchTerm = '';
    this.page = 1;
    this.loadUsers();
  }

  refreshUsers(): void {
    this.loadUsers();
  }

  goToPage(page: number): void {
    if (page < 1 || page > this.pages || page === this.page) {
      return;
    }

    this.page = page;
    this.loadUsers();
  }

  isSelf(user: AdminUser | null): boolean {
    return Boolean(this.user && user && this.user.id === user.id);
  }

  openEdit(user: AdminUser): void {
    this.editUser = user;
    this.editError = '';
    this.editForm.reset({
      name: user.name,
      email: user.email
    });
  }

  closeEdit(): void {
    this.editUser = null;
    this.editError = '';
    this.saving = false;
  }

  saveEdit(): void {
    if (!this.editUser || this.saving) {
      return;
    }

    if (this.editForm.invalid) {
      this.editForm.markAllAsTouched();
      return;
    }

    const values = this.editForm.getRawValue() as {
      name: string;
      email: string;
    };
    const name = values.name.trim();
    const email = values.email.trim();

    if (!name) {
      this.editError = 'Name is required.';
      return;
    }

    const emailChanged =
      email.toLowerCase() !== this.editUser.email.toLowerCase();
    const payload: AdminUserUpdate = { name, email };

    this.saving = true;
    this.editError = '';
    this.successMessage = '';

    this.auth.updateAdminUser(this.editUser.id, payload).subscribe({
      next: (updated) => {
        this.saving = false;
        this.successMessage = emailChanged
          ? 'User updated. The new email address must be verified before the user can sign in again.'
          : 'User updated successfully.';
        this.editUser = null;

        if (this.user && this.user.id === updated.id) {
          this.user = {
            ...this.user,
            name: updated.name,
            email: updated.email
          };
        }

        this.loadUsers();
      },
      error: (error) => {
        this.saving = false;
        this.editError = this.apiErrorMessage(
          error,
          'Unable to update this user.'
        );
      }
    });
  }

  toggleBlock(user: AdminUser): void {
    if (this.isSelf(user) || this.actionUserId !== null) {
      return;
    }

    this.actionUserId = user.id;
    this.actionError = '';
    this.successMessage = '';

    const request = user.is_blocked
      ? this.auth.unblockAdminUser(user.id)
      : this.auth.blockAdminUser(user.id);

    request.subscribe({
      next: () => {
        this.actionUserId = null;
        this.successMessage = user.is_blocked
          ? 'User unblocked successfully.'
          : 'User blocked successfully. Existing sessions can no longer access protected endpoints.';
        this.loadUsers();
      },
      error: (error) => {
        this.actionUserId = null;
        this.actionError = this.apiErrorMessage(
          error,
          'Unable to change this account status.'
        );
      }
    });
  }

  askDelete(user: AdminUser): void {
    if (this.isSelf(user)) {
      return;
    }

    this.deleteUser = user;
    this.deleteError = '';
    this.successMessage = '';
  }

  cancelDelete(): void {
    this.deleteUser = null;
    this.deleteError = '';
    this.deleting = false;
  }

  confirmDelete(): void {
    if (!this.deleteUser || this.deleting) {
      return;
    }

    const target = this.deleteUser;
    this.deleting = true;
    this.deleteError = '';

    this.auth.deleteAdminUser(target.id).subscribe({
      next: (response) => {
        this.deleting = false;
        this.deleteUser = null;
        this.successMessage =
          response.message || 'User deleted successfully.';

        if (this.detailsUser && this.detailsUser.id === target.id) {
          this.closeDetails();
        }

        if (this.users.length === 1 && this.page > 1) {
          this.page -= 1;
        }

        this.loadUsers();
      },
      error: (error) => {
        this.deleting = false;
        this.deleteError = this.apiErrorMessage(
          error,
          'Unable to delete this user.'
        );
      }
    });
  }

  viewUser(user: AdminUser): void {
    this.detailsUser = user;
    this.loginHistory = null;
    this.historyError = '';
    this.historyLoading = true;

    this.auth.getAdminUserLogins(user.id, { pageSize: 20 }).subscribe({
      next: (history) => {
        this.loginHistory = history;
        this.historyLoading = false;
      },
      error: (error) => {
        this.historyLoading = false;
        this.historyError = this.apiErrorMessage(
          error,
          'Unable to load login history.'
        );
      }
    });
  }

  closeDetails(): void {
    this.detailsUser = null;
    this.loginHistory = null;
    this.historyError = '';
    this.historyLoading = false;
  }

  statusLabel(status: AdminUserStatus): string {
    if (status === 'active') {
      return 'Active';
    }

    if (status === 'blocked') {
      return 'Blocked';
    }

    return 'Inactive';
  }

  statusClasses(status: AdminUserStatus): string {
    if (status === 'active') {
      return 'border-emerald-400/20 bg-emerald-400/[0.08] text-emerald-300';
    }

    if (status === 'blocked') {
      return 'border-rose-400/20 bg-rose-400/[0.08] text-rose-300';
    }

    return 'border-amber-400/20 bg-amber-400/[0.08] text-amber-300';
  }

  roleClasses(role: 'admin' | 'user'): string {
    return role === 'admin'
      ? 'border-amber-300/20 bg-amber-300/[0.08] text-amber-300'
      : 'border-blue-300/20 bg-blue-400/[0.08] text-blue-300';
  }

  providerLabel(provider: string): string {
    if (provider === 'password') {
      return 'Email & password';
    }

    if (provider === 'google') {
      return 'Google';
    }

    if (provider === 'google+password') {
      return 'Google + password';
    }

    return 'Unknown';
  }

  methodLabel(method: string | null): string {
    if (method === 'password') {
      return 'Password';
    }

    if (method === 'google') {
      return 'Google';
    }

    return 'Not available';
  }

  logout(): void {
    this.auth.logout();
    void this.router.navigate(['/auth']);
  }

  private apiErrorMessage(error: unknown, fallback: string): string {
    const httpError = error as {
      status?: number;
      error?: { detail?: unknown };
    };

    if (httpError.status === 401) {
      this.auth.clearSession();
      void this.router.navigate(['/auth']);
      return 'Your session has expired. Please sign in again.';
    }

    if (httpError.status === 403) {
      void this.router.navigate(['/dashboard']);
      return 'Administrator access is required.';
    }

    if (typeof httpError.error?.detail === 'string') {
      return httpError.error.detail;
    }

    return fallback;
  }
}
