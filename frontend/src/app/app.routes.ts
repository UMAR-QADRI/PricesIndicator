import { Routes } from '@angular/router';
import { AdminDashboard } from '../admindashboard/admindashboard';
import { Auth } from '../auth/auth';
import { adminGuard, authGuard } from '../auth/auth.guard';
import { Otp } from '../otp/otp';
import { UserDashboard } from '../userdashboard/userdashboard';

export const routes: Routes = [
  { path: 'auth', component: Auth },
  { path: 'otp', component: Otp },

  {
    path: 'dashboard',
    component: UserDashboard,
    canActivate: [authGuard]
  },
  {
    path: 'admin',
    component: AdminDashboard,
    canActivate: [authGuard, adminGuard]
  },

  // Backwards-compatible aliases for links created by earlier builds.


  { path: '', redirectTo: 'auth', pathMatch: 'full' },
  { path: '**', redirectTo: 'auth' }
];
