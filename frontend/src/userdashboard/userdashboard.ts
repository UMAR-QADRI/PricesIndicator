
import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { AuthServ, AuthUser } from '../auth-serv';

@Component({
  selector: 'app-user-dashboard',
  standalone: true,
  imports: [CommonModule, RouterLink],
  templateUrl: './userdashboard.html'
})
export class UserDashboard implements OnInit {
  user: AuthUser | null = null;
  loading = true;
  errorMessage = '';

  constructor(
    private auth: AuthServ,
    private router: Router
  ) {}

  ngOnInit(): void {
    this.loadAccount();
  }

  loadAccount(): void {
    this.loading = true;
    this.errorMessage = '';

    this.auth.getMe().subscribe({
      next: (user) => {
        if (user.role === 'admin') {
          void this.router.navigate(['/admin']);
          return;
        }

        this.user = user;
        this.loading = false;
      },
      error: (error) => {
        this.loading = false;

        if (error.status === 401) {
          this.auth.clearSession();
          void this.router.navigate(['/auth']);
          return;
        }

        this.errorMessage =
          'Unable to load your account right now. Please try again.';
      }
    });
  }

  logout(): void {
    this.auth.logout();
    void this.router.navigate(['/auth']);
  }
}
