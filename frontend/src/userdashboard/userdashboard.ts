import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import {
  AuthServ,
  AuthUser,
  ScraperInfo,
  ScraperProduct
} from '../auth-serv';

interface ScraperResultGroup {
  scraper: ScraperInfo;
  products: ScraperProduct[];
}

interface ScraperErrorGroup {
  scraper: ScraperInfo;
  message: string;
}

@Component({
  selector: 'app-user-dashboard',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  templateUrl: './userdashboard.html'
})
export class UserDashboard implements OnInit {
  user: AuthUser | null = null;
  loading = true;
  errorMessage = '';

  scrapers: ScraperInfo[] = [];
  scrapersLoading = false;
  scrapersError = '';
  selectedScraperIds: string[] = [];

  keyword = '';
  lastKeyword = '';
  searching = false;
  hasSearched = false;
  searchError = '';
  resultGroups: ScraperResultGroup[] = [];
  errorGroups: ScraperErrorGroup[] = [];

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
        this.loadScrapers();
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

  loadScrapers(): void {
    this.scrapersLoading = true;
    this.scrapersError = '';

    this.auth.getScrapers().subscribe({
      next: (response) => {
        this.scrapers = response.scrapers.filter(
          (scraper) => scraper.enabled
        );
        this.selectedScraperIds = this.scrapers.map((scraper) => scraper.id);
        this.scrapersLoading = false;
      },
      error: (error) => {
        this.scrapersLoading = false;
        this.scrapers = [];
        this.selectedScraperIds = [];
        this.scrapersError = this.apiErrorMessage(
          error,
          'Unable to load the available scrapers.'
        );
      }
    });
  }

  toggleScraper(scraperId: string, event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;

    if (checked && !this.selectedScraperIds.includes(scraperId)) {
      this.selectedScraperIds = [...this.selectedScraperIds, scraperId];
      return;
    }

    if (!checked) {
      this.selectedScraperIds = this.selectedScraperIds.filter(
        (id) => id !== scraperId
      );
    }
  }

  isScraperSelected(scraperId: string): boolean {
    return this.selectedScraperIds.includes(scraperId);
  }

  search(): void {
    if (this.searching) {
      return;
    }

    const keyword = this.keyword.trim();
    this.searchError = '';

    if (!keyword) {
      this.searchError = 'Enter a product keyword.';
      return;
    }

    if (this.selectedScraperIds.length === 0) {
      this.searchError = 'Select at least one scraper.';
      return;
    }

    this.searching = true;
    this.hasSearched = true;
    this.lastKeyword = keyword;
    this.resultGroups = [];
    this.errorGroups = [];

    this.auth.searchScrapers(keyword, this.selectedScraperIds).subscribe({
      next: (response) => {
        const selectedScrapers = this.selectedScraperIds
          .map((id) => this.scrapers.find((scraper) => scraper.id === id))
          .filter((scraper): scraper is ScraperInfo => Boolean(scraper));

        this.resultGroups = selectedScrapers
          .map((scraper) => ({
            scraper,
            products: response.results?.[scraper.id] ?? []
          }))
          .filter((group) => group.products.length > 0);

        this.errorGroups = selectedScrapers
          .filter((scraper) => Boolean(response.errors?.[scraper.id]))
          .map((scraper) => ({
            scraper,
            message:
              response.errors?.[scraper.id] ?? 'The scraper failed.'
          }));

        this.searching = false;
      },
      error: (error) => {
        this.searching = false;
        this.searchError = this.apiErrorMessage(
          error,
          'Unable to search the selected scrapers. Please try again.'
        );
      }
    });
  }

  get totalProducts(): number {
    return this.resultGroups.reduce(
      (total, group) => total + group.products.length,
      0
    );
  }

  productTitle(product: ScraperProduct): string {
    return product.title?.trim() || 'Product name unavailable';
  }

  productImage(product: ScraperProduct): string {
    if (product.image) {
      return product.image;
    }

    return product.images?.[0] ?? '';
  }

  productUrl(product: ScraperProduct): string {
    return product.product_url || product.url || '';
  }

  productPrice(product: ScraperProduct): string {
    if (!this.hasValue(product.price)) {
      return '';
    }

    return `${product.currency ?? ''} ${product.price}`.trim();
  }

  productOriginalPrice(product: ScraperProduct): string {
    if (!this.hasValue(product.original_price)) {
      return '';
    }

    return `${product.currency ?? ''} ${product.original_price}`.trim();
  }

  hasOriginalPrice(product: ScraperProduct): boolean {
    if (!this.hasValue(product.original_price)) {
      return false;
    }

    if (!this.hasValue(product.price)) {
      return true;
    }

    return String(product.original_price) !== String(product.price);
  }

  reviewCount(product: ScraperProduct): string | number | null {
    return product.review_count ?? product.rating_count ?? null;
  }

  hasValue(value: unknown): boolean {
    return value !== null && value !== undefined && value !== '';
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

    if (httpError.status === 401 || httpError.status === 403) {
      this.auth.clearSession();
      void this.router.navigate(['/auth']);
      return 'Your session has expired. Please sign in again.';
    }

    if (typeof httpError.error?.detail === 'string') {
      return httpError.error.detail;
    }

    if (Array.isArray(httpError.error?.detail)) {
      const first = httpError.error.detail[0] as { msg?: unknown } | undefined;

      if (typeof first?.msg === 'string') {
        return first.msg;
      }
    }

    return fallback;
  }
}
