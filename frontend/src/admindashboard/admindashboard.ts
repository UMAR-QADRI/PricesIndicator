
import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { Router } from '@angular/router';
import { AuthServ, AuthUser } from '../auth-serv';

@Component({
  selector: 'app-admin-dashboard',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './admindashboard.html'
})
export class AdminDashboard implements OnInit {
  user: AuthUser | null = null;
  loading = true;
  errorMessage = '';

  constructor(
    private auth: AuthServ,
    private router: Router
  ) {}

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

  logout(): void {
    this.auth.logout();
    void this.router.navigate(['/auth']);
  }
}
