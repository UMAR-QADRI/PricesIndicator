import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { catchError, map, of } from 'rxjs';

import { AuthServ } from '../auth-serv';

export const authGuard: CanActivateFn = () => {
  const auth = inject(AuthServ);
  const router = inject(Router);

  if (!auth.getToken()) {
    return router.createUrlTree(['/auth']);
  }

  return true;
};

export const adminGuard: CanActivateFn = () => {
  const auth = inject(AuthServ);
  const router = inject(Router);

  if (!auth.getToken()) {
    return router.createUrlTree(['/auth']);
  }

  return auth.getMe().pipe(
    map((user) =>
      user.role === 'admin'
        ? true
        : router.createUrlTree(['/admindashboard'])
    ),
    catchError(() => {
      auth.clearSession();
      return of(router.createUrlTree(['/auth']));
    })
  );
};
